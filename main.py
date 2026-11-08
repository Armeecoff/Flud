"""
Флуд-бот: aiogram 3.x + aiosqlite
Установка:  pip install "aiogram>=3.7" aiosqlite
Запуск:     python flood_bot.py
"""
import asyncio
import html
import logging
import math
import os
import random
import secrets
import time
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, ChatJoinRequest, ChatMemberUpdated,
                           KeyboardButton, Message, ReplyKeyboardMarkup)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ======================= НАСТРОЙКИ =======================
BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_TOKEN_HERE")
GROUP_ID = int(os.getenv("GROUP_ID", "-1001234567890"))   # ID флуд-группы
OWNER_ID = int(os.getenv("OWNER_ID", "123456789"))        # ID владельца (добавляет админов)

SOURCE_CHANNEL = "@furinarn_info"   # откуда копируется пост
SOURCE_POST_ID = 5
READ_SECONDS = 30                   # сколько секунд нельзя нажать "Ознакомлен"
START_BALANCE = 100                 # монет при принятии
COMMISSION = 0.10                   # комиссия с ставки соперника
MSG_COOLDOWN = 3                    # антиспам: засчитываем не чаще 1 сообщения в N сек
GAME_TIMEOUT = 120                  # секунд бездействия в групповой игре
TZ = ZoneInfo("Europe/Moscow")      # по нему считаются "сутки" и "неделя"
DB_PATH = "flood.db"

CASINO_MULT_TRIPLE = 10             # три одинаковых в 🎰
CASINO_MULT_JACKPOT = 30            # 777
GUESS_COEF = {3: 2.7, 5: 4.5, 10: 9.0}   # диапазон 1..N -> коэффициент

KINDS = {  # kind: (шаблон названия, нужно ли число)
    "msgs_day": ("Написать {n} сообщений за день", True),
    "msgs_week": ("Написать {n} сообщений за неделю", True),
    "msgs_total": ("Написать {n} сообщений всего", True),
    "days_in": ("Пробыть во флуде {n} дней", True),
    "streak": ("Писать во флуде {n} дней подряд", True),
    "games_win": ("Выиграть {n} игр в группе", True),
    "manual": ("Необязательный интерактив", False),
}
# =========================================================

logging.basicConfig(level=logging.INFO)
bot: Bot
db: aiosqlite.Connection
ADMINS: set[int] = set()
last_counted: dict[int, float] = {}
pending: dict[str, dict] = {}   # неподтверждённые вызовы в группе
games: dict[str, dict] = {}     # идущие групповые игры
bg_tasks: set = set()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
  status TEXT DEFAULT 'new', prev_status TEXT, role TEXT,
  balance INTEGER DEFAULT 0, code TEXT, start_ts REAL DEFAULT 0,
  invite_link TEXT, joined_at TEXT, msgs_total INTEGER DEFAULT 0,
  wins INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS msg_stats(user_id INTEGER, day TEXT, cnt INTEGER DEFAULT 0,
  PRIMARY KEY(user_id, day));
CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, title TEXT,
  description TEXT, target INTEGER DEFAULT 0, reward INTEGER, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS claims(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER,
  user_id INTEGER, period TEXT, status TEXT, created_at TEXT,
  UNIQUE(task_id, user_id, period));
CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, price INTEGER,
  stock INTEGER DEFAULT 0, unlimited INTEGER DEFAULT 0, description TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS purchases(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
  item_id INTEGER, name TEXT, price INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS game_counts(user_id INTEGER, day TEXT, cnt INTEGER DEFAULT 0,
  PRIMARY KEY(user_id, day));
CREATE TABLE IF NOT EXISTS active_games(id TEXT PRIMARY KEY, p1 INTEGER, p2 INTEGER, bet INTEGER);
"""


# ----------------------- DB helpers -----------------------
async def q_one(sql, args=()):
    async with db.execute(sql, args) as c:
        return await c.fetchone()


async def q_all(sql, args=()):
    async with db.execute(sql, args) as c:
        return await c.fetchall()


async def ex(sql, args=()):
    cur = await db.execute(sql, args)
    await db.commit()
    return cur.rowcount


async def get_user(uid):
    return await q_one("SELECT * FROM users WHERE user_id=?", (uid,))


async def get_setting(key, default):
    r = await q_one("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else default


async def set_setting(key, value):
    await ex("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
             (key, str(value)))


async def add_balance(uid, n):
    await ex("UPDATE users SET balance=balance+? WHERE user_id=?", (n, uid))


async def take_balance(uid, n) -> bool:
    return await ex("UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?", (n, uid, n)) > 0


def is_admin(uid) -> bool:
    return uid == OWNER_ID or uid in ADMINS


def today() -> str:
    return datetime.now(TZ).date().isoformat()


def week_start() -> str:
    d = datetime.now(TZ).date()
    return (d - timedelta(days=d.weekday())).isoformat()


def week_key() -> str:
    y, w, _ = datetime.now(TZ).isocalendar()
    return f"{y}-W{w:02d}"


def esc(s) -> str:
    return html.escape(str(s or ""))


def uname(u) -> str:
    s = esc(u["full_name"] or u["user_id"])
    return s + (f" (@{esc(u['username'])})" if u["username"] else "") + f" [<code>{u['user_id']}</code>]"


def spawn(coro):
    t = asyncio.create_task(coro)
    bg_tasks.add(t)
    t.add_done_callback(bg_tasks.discard)


async def ensure_user(tg_user):
    uid = tg_user.id
    adm = is_admin(uid)
    await db.execute(
        "INSERT OR IGNORE INTO users(user_id,status,balance,created_at) VALUES(?,?,?,?)",
        (uid, "member" if adm else "new", START_BALANCE if adm else 0, today()))
    await db.execute("UPDATE users SET username=?, full_name=? WHERE user_id=?",
                     (tg_user.username, tg_user.full_name, uid))
    if adm:
        await db.execute("UPDATE users SET status='member' WHERE user_id=? AND status IN ('new','agreed','declined')", (uid,))
    await db.commit()


async def find_user(text):
    t = (text or "").strip().lstrip("@")
    if t.isdigit():
        return await get_user(int(t))
    return await q_one("SELECT * FROM users WHERE lower(username)=lower(?)", (t,))


async def notify_admins(text, kb=None):
    for aid in {OWNER_ID, *ADMINS}:
        try:
            await bot.send_message(aid, text, reply_markup=kb)
        except Exception:
            pass


async def new_link(uid) -> str:
    link = await bot.create_chat_invite_link(GROUP_ID, name=f"user-{uid}", creates_join_request=True)
    await ex("UPDATE users SET invite_link=? WHERE user_id=?", (link.invite_link, uid))
    return link.invite_link


def main_kb(uid):
    rows = []
    if is_admin(uid):
        rows += [["➕ Добавить Нью", "📊 Статистика"],
                 ["🪙 Монеты", "🚫 Бан / Разбан"],
                 ["📝 Управление заданиями", "🛍 Управление магазином"],
                 ["⚙️ Настройки"] + (["👑 Админы"] if uid == OWNER_ID else [])]
    rows += [["👤 Профиль", "📝 Задания"], ["🛒 Магазин", "🎮 Игры"]]
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in r] for r in rows], resize_keyboard=True)


# ----------------------- фильтры / middleware -----------------------
class IsAdmin(BaseFilter):
    async def __call__(self, event) -> bool:
        return is_admin(event.from_user.id)


class IsMember(BaseFilter):
    async def __call__(self, event) -> bool:
        u = await get_user(event.from_user.id)
        return bool(u and u["status"] == "member")


class Guard(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = event.from_user
        private = isinstance(event, CallbackQuery) or event.chat.type == ChatType.PRIVATE
        if user and not user.is_bot and private:
            await ensure_user(user)
            u = await get_user(user.id)
            if u["status"] == "banned" and not is_admin(user.id):
                if isinstance(event, CallbackQuery):
                    await event.answer("Вы заблокированы.", show_alert=True)
                else:
                    await event.answer("🚫 Вы заблокированы.")
                return
        return await handler(event, data)


# ----------------------- FSM -----------------------
class AddNew(StatesGroup):
    code = State(); role = State()
class Coins(StatesGroup):
    target = State(); action = State(); amount = State()
class BanS(StatesGroup):
    target = State()
class TaskAdd(StatesGroup):
    kind = State(); target = State(); title = State(); reward = State(); desc = State()
class ItemAdd(StatesGroup):
    name = State(); price = State(); qty = State(); desc = State()
class AdminS(StatesGroup):
    add = State(); remove = State()
class Cfg(StatesGroup):
    limit = State()
class Game(StatesGroup):
    casino = State(); guess_bet = State(); guess_diff = State(); guess_pick = State()


private = Router()
private.message.filter(F.chat.type == ChatType.PRIVATE)
private.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
grp = Router()
grp.message.filter(F.chat.id == GROUP_ID)
grp.callback_query.filter(F.message.chat.id == GROUP_ID)
misc = Router()


# =====================================================================
#                          РЕГИСТРАЦИЯ НЬЮ
# =====================================================================
def rules_kb():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Ознакомлен", callback_data="rules:ok")
    b.button(text="❌ Отказываюсь", callback_data="rules:no")
    b.adjust(2)
    return b.as_markup()


@private.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    u = await get_user(uid)
    if u["status"] == "member":
        await m.answer("С возвращением! Меню ниже 👇", reply_markup=main_kb(uid))
        return
    if u["status"] == "agreed":
        await m.answer(f"Ваш код: <code>{u['code']}</code>\nОжидайте, пока администратор вас подтвердит.")
        return
    await ex("UPDATE users SET status='new', start_ts=? WHERE user_id=?", (time.time(), uid))
    try:
        await bot.copy_message(m.chat.id, SOURCE_CHANNEL, SOURCE_POST_ID, reply_markup=rules_kb())
    except Exception:
        logging.exception("copy_message failed")
        await m.answer(f"Ознакомьтесь с постом: https://t.me/{SOURCE_CHANNEL.lstrip('@')}/{SOURCE_POST_ID}",
                       reply_markup=rules_kb())


@private.callback_query(F.data == "rules:ok")
async def rules_ok(c: CallbackQuery):
    u = await get_user(c.from_user.id)
    if u["status"] not in ("new", "declined"):
        await c.answer()
        return
    left = READ_SECONDS - (time.time() - (u["start_ts"] or 0))
    if left > 0:
        await c.answer(f"У вас осталось {math.ceil(left)} секунд до подтверждения", show_alert=True)
        return
    while True:
        code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
        if not await q_one("SELECT 1 FROM users WHERE code=?", (code,)):
            break
    await ex("UPDATE users SET status='agreed', code=? WHERE user_id=?", (code, c.from_user.id))
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer(f"Ваш уникальный код: <code>{code}</code>\nПередайте его администратору.\n\n⏳ Ожидайте.")
    await c.answer()


@private.callback_query(F.data == "rules:no")
async def rules_no(c: CallbackQuery):
    u = await get_user(c.from_user.id)
    if u["status"] in ("new", "declined"):
        await ex("UPDATE users SET status='declined' WHERE user_id=?", (c.from_user.id,))
        await c.message.edit_reply_markup(reply_markup=None)
        await c.message.answer("Вы отказались. Если передумаете — отправьте /start.")
    await c.answer()


@private.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Отменено.", reply_markup=main_kb(m.from_user.id))


# ----- Добавить Нью (админ) -----
@private.message(F.text == "➕ Добавить Нью", IsAdmin())
async def add_new_start(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(AddNew.code)
    await m.answer("Отправьте код, который выдал бот Нью. (/cancel — отмена)")


@private.message(AddNew.code)
async def add_new_code(m: Message, state: FSMContext):
    code = (m.text or "").strip().upper()
    u = await q_one("SELECT * FROM users WHERE code=? AND status='agreed'", (code,))
    if not u:
        await m.answer("❌ Код не найден или уже использован. Попробуйте ещё раз или /cancel.")
        return
    await state.update_data(uid=u["user_id"])
    await state.set_state(AddNew.role)
    await m.answer(f"Код верный: {uname(u)}\nВведите роль для этого Нью:")


@private.message(AddNew.role)
async def add_new_role(m: Message, state: FSMContext):
    role = (m.text or "").strip()[:64]
    uid = (await state.get_data())["uid"]
    try:
        link = await new_link(uid)
    except Exception as e:
        await m.answer(f"⚠️ Не удалось создать ссылку: {esc(e)}\nПроверьте, что бот админ в группе с правом приглашать.")
        await state.clear()
        return
    await ex("UPDATE users SET status='member', role=?, balance=balance+?, code=NULL WHERE user_id=?",
             (role, START_BALANCE, uid))
    await state.clear()
    try:
        await bot.send_message(
            uid, f"🎉 Вас добавили! Ваша роль: <b>{esc(role)}</b>\n"
                 f"Начислено {START_BALANCE} 🪙\n\n"
                 f"Ваша одноразовая ссылка для вступления в группу:\n{link}",
            reply_markup=main_kb(uid))
    except Exception:
        await m.answer("⚠️ Не удалось написать пользователю (он мог заблокировать бота).")
    await m.answer("✅ Нью добавлен, ссылка отправлена.", reply_markup=main_kb(m.from_user.id))


# ----- вступление в группу -----
@misc.chat_join_request(F.chat.id == GROUP_ID)
async def on_join_request(req: ChatJoinRequest):
    uid = req.from_user.id
    u = await get_user(uid)
    link = req.invite_link.invite_link if req.invite_link else None
    ok = is_admin(uid) or bool(u and u["status"] == "member" and u["invite_link"] and link == u["invite_link"])
    if not ok:
        await req.decline()
        return
    await req.approve()
    if u and link and link == u["invite_link"]:
        try:
            await bot.revoke_chat_invite_link(GROUP_ID, link)   # одноразовость
        except Exception:
            pass
        await ex("UPDATE users SET invite_link=NULL, joined_at=COALESCE(joined_at,?) WHERE user_id=?", (today(), uid))


# ----- синхронизация бана группа -> бот -----
@misc.chat_member(F.chat.id == GROUP_ID)
async def on_chat_member(ev: ChatMemberUpdated):
    uid = ev.new_chat_member.user.id
    u = await get_user(uid)
    if not u or is_admin(uid):
        return
    if ev.new_chat_member.status == "kicked" and u["status"] != "banned":
        await do_ban(uid, tg=False)
    elif ev.old_chat_member.status == "kicked" and ev.new_chat_member.status != "kicked" and u["status"] == "banned":
        await do_unban(uid, tg=False)


async def do_ban(uid, tg=True):
    await ex("UPDATE users SET prev_status=status, status='banned' WHERE user_id=? AND status!='banned'", (uid,))
    if tg:
        try:
            await bot.ban_chat_member(GROUP_ID, uid)
        except Exception:
            logging.exception("ban in group failed")
    try:
        await bot.send_message(uid, "🚫 Вы заблокированы.")
    except Exception:
        pass


async def do_unban(uid, tg=True):
    u = await get_user(uid)
    prev = u["prev_status"] if u and u["prev_status"] in ("member", "agreed", "new") else "new"
    await ex("UPDATE users SET status=?, prev_status=NULL WHERE user_id=?", (prev, uid))
    if tg:
        try:
            await bot.unban_chat_member(GROUP_ID, uid, only_if_banned=True)
        except Exception:
            logging.exception("unban in group failed")
    try:
        if prev == "member":
            link = await new_link(uid)
            await bot.send_message(uid, f"✅ Вы разблокированы. Новая одноразовая ссылка:\n{link}",
                                   reply_markup=main_kb(uid))
        else:
            await bot.send_message(uid, "✅ Вы разблокированы. Отправьте /start.")
    except Exception:
        pass


# =====================================================================
#                          ПРОФИЛЬ / ЗАДАНИЯ
# =====================================================================
async def games_left(uid) -> int:
    lim = int(await get_setting("games_per_day", "5"))
    if lim == 0:
        return 10 ** 9
    r = await q_one("SELECT cnt FROM game_counts WHERE user_id=? AND day=?", (uid, today()))
    return lim - (r["cnt"] if r else 0)


@private.message(F.text == "👤 Профиль", IsMember())
async def profile(m: Message, state: FSMContext):
    await state.clear()
    u = await get_user(m.from_user.id)
    day = await q_one("SELECT cnt FROM msg_stats WHERE user_id=? AND day=?", (u["user_id"], today()))
    wk = await q_one("SELECT COALESCE(SUM(cnt),0) s FROM msg_stats WHERE user_id=? AND day>=?",
                     (u["user_id"], week_start()))
    left = await games_left(u["user_id"])
    days_in = (datetime.now(TZ).date() - date.fromisoformat(u["joined_at"])).days if u["joined_at"] else 0
    await m.answer(
        f"👤 <b>{esc(u['full_name'])}</b>\n"
        f"Роль: {esc(u['role']) or '—'}\n"
        f"💰 Баланс: <b>{u['balance']}</b> 🪙\n"
        f"📅 Во флуде дней: {days_in}\n"
        f"✉️ Сообщений: сегодня {day['cnt'] if day else 0}, за неделю {wk['s']}, всего {u['msgs_total']}\n"
        f"🏆 Побед в играх: {u['wins']}\n"
        f"🎮 Игр в группе сегодня осталось: {'∞' if left > 10**6 else max(left, 0)}")


def task_period(t) -> str:
    return {"msgs_day": today(), "msgs_week": week_key()}.get(t["kind"], "once")


async def streak(uid) -> int:
    rows = await q_all("SELECT day FROM msg_stats WHERE user_id=? ORDER BY day DESC LIMIT 400", (uid,))
    days = {r["day"] for r in rows}
    d = datetime.now(TZ).date()
    if d.isoformat() not in days:
        d -= timedelta(days=1)
    n = 0
    while d.isoformat() in days:
        n += 1
        d -= timedelta(days=1)
    return n


async def progress(t, uid) -> int:
    k = t["kind"]
    if k == "msgs_day":
        r = await q_one("SELECT cnt FROM msg_stats WHERE user_id=? AND day=?", (uid, today()))
        return r["cnt"] if r else 0
    if k == "msgs_week":
        r = await q_one("SELECT COALESCE(SUM(cnt),0) s FROM msg_stats WHERE user_id=? AND day>=?", (uid, week_start()))
        return r["s"]
    u = await get_user(uid)
    if k == "msgs_total":
        return u["msgs_total"]
    if k == "games_win":
        return u["wins"]
    if k == "days_in":
        return (datetime.now(TZ).date() - date.fromisoformat(u["joined_at"])).days if u["joined_at"] else 0
    if k == "streak":
        return await streak(uid)
    return 0


async def tasks_view(uid):
    ts = await q_all("SELECT * FROM tasks WHERE active=1 ORDER BY id")
    if not ts:
        return "Заданий пока нет.", None
    lines, kb = [], InlineKeyboardBuilder()
    for t in ts:
        cl = await q_one("SELECT status FROM claims WHERE task_id=? AND user_id=? AND period=?",
                         (t["id"], uid, task_period(t)))
        per = {"msgs_day": " (каждый день)", "msgs_week": " (каждую неделю)"}.get(t["kind"], "")
        s = f"<b>#{t['id']}</b> {esc(t['title'])}{per}\nНаграда: {t['reward']} 🪙"
        if t["description"]:
            s += f"\n{esc(t['description'])}"
        if cl and cl["status"] == "done":
            s += "\n✅ Выполнено"
        elif cl:
            s += "\n⏳ На проверке"
        else:
            if t["kind"] != "manual":
                s += f"\nПрогресс: {await progress(t, uid)}/{t['target']}"
            kb.button(text=f"Сдать #{t['id']}", callback_data=f"task:{t['id']}")
        lines.append(s)
    kb.adjust(3)
    return "📝 <b>Задания</b>\n\n" + "\n\n".join(lines), kb.as_markup()


@private.message(F.text == "📝 Задания", IsMember())
async def tasks_list(m: Message, state: FSMContext):
    await state.clear()
    text, kb = await tasks_view(m.from_user.id)
    await m.answer(text, reply_markup=kb)


@private.callback_query(F.data.startswith("task:"), IsMember())
async def task_claim(c: CallbackQuery):
    uid = c.from_user.id
    t = await q_one("SELECT * FROM tasks WHERE id=? AND active=1", (int(c.data.split(":")[1]),))
    if not t:
        await c.answer("Задание недоступно", show_alert=True)
        return
    period = task_period(t)
    if await q_one("SELECT 1 FROM claims WHERE task_id=? AND user_id=? AND period=?", (t["id"], uid, period)):
        await c.answer("Уже сдано", show_alert=True)
        return
    if t["kind"] == "manual":
        await ex("INSERT INTO claims(task_id,user_id,period,status,created_at) VALUES(?,?,?,?,?)",
                 (t["id"], uid, period, "pending", today()))
        cl = await q_one("SELECT id FROM claims WHERE task_id=? AND user_id=? AND period=?", (t["id"], uid, period))
        u = await get_user(uid)
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Принять", callback_data=f"mt:ok:{cl['id']}")
        kb.button(text="❌ Отклонить", callback_data=f"mt:no:{cl['id']}")
        await notify_admins(f"📥 Задание на проверку #{t['id']} «{esc(t['title'])}»\nОт: {uname(u)}", kb.as_markup())
        await c.answer("Отправлено админам на проверку", show_alert=True)
    else:
        cur = await progress(t, uid)
        if cur < t["target"]:
            await c.answer(f"Пока не выполнено: {cur}/{t['target']}", show_alert=True)
            return
        try:
            await ex("INSERT INTO claims(task_id,user_id,period,status,created_at) VALUES(?,?,?,?,?)",
                     (t["id"], uid, period, "done", today()))
        except aiosqlite.IntegrityError:
            await c.answer("Уже сдано", show_alert=True)
            return
        await add_balance(uid, t["reward"])
        await c.answer(f"Выполнено! +{t['reward']} 🪙", show_alert=True)
    try:
        text, kb = await tasks_view(uid)
        await c.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass


@private.callback_query(F.data.startswith("mt:"), IsAdmin())
async def manual_review(c: CallbackQuery):
    _, act, cid = c.data.split(":")
    cl = await q_one("SELECT c.*, t.title, t.reward FROM claims c JOIN tasks t ON t.id=c.task_id WHERE c.id=?", (int(cid),))
    if not cl or cl["status"] != "pending":
        await c.answer("Уже обработано", show_alert=True)
        return
    if act == "ok":
        await ex("UPDATE claims SET status='done' WHERE id=? AND status='pending'", (cl["id"],))
        await add_balance(cl["user_id"], cl["reward"])
        msg, mark = f"✅ Задание «{esc(cl['title'])}» принято! +{cl['reward']} 🪙", "✅ Принято"
    else:
        await ex("DELETE FROM claims WHERE id=?", (cl["id"],))
        msg, mark = f"❌ Задание «{esc(cl['title'])}» отклонено. Можно попробовать снова.", "❌ Отклонено"
    try:
        await bot.send_message(cl["user_id"], msg)
    except Exception:
        pass
    await c.message.edit_text(f"{c.message.html_text}\n\n{mark} ({esc(c.from_user.full_name)})")
    await c.answer()


# =====================================================================
#                             МАГАЗИН
# =====================================================================
@private.message(F.text == "🛒 Магазин", IsMember())
async def shop(m: Message, state: FSMContext):
    await state.clear()
    items = await q_all("SELECT * FROM items WHERE active=1 ORDER BY id")
    if not items:
        await m.answer("Магазин пока пуст.")
        return
    u = await get_user(m.from_user.id)
    lines, kb = [], InlineKeyboardBuilder()
    for i in items:
        stock = "∞" if i["unlimited"] else str(i["stock"])
        lines.append(f"<b>{esc(i['name'])}</b> — {i['price']} 🪙 (осталось: {stock})\n{esc(i['description'])}")
        kb.button(text=f"{i['name']} — {i['price']}🪙", callback_data=f"buy:{i['id']}")
    kb.adjust(1)
    await m.answer(f"🛒 <b>Магазин</b>  |  Баланс: {u['balance']} 🪙\n\n" + "\n\n".join(lines), reply_markup=kb.as_markup())


@private.callback_query(F.data.startswith("buy:"), IsMember())
async def buy_ask(c: CallbackQuery):
    i = await q_one("SELECT * FROM items WHERE id=? AND active=1", (int(c.data.split(":")[1]),))
    if not i:
        await c.answer("Товар недоступен", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Купить", callback_data=f"buyok:{i['id']}")
    kb.button(text="✖️ Отмена", callback_data="buyno")
    await c.message.answer(f"Купить «{esc(i['name'])}» за {i['price']} 🪙?", reply_markup=kb.as_markup())
    await c.answer()


@private.callback_query(F.data == "buyno")
async def buy_no(c: CallbackQuery):
    await c.message.delete()
    await c.answer()


@private.callback_query(F.data.startswith("buyok:"), IsMember())
async def buy_ok(c: CallbackQuery):
    uid = c.from_user.id
    i = await q_one("SELECT * FROM items WHERE id=? AND active=1", (int(c.data.split(":")[1]),))
    if not i:
        await c.answer("Товар недоступен", show_alert=True)
        return
    if not await take_balance(uid, i["price"]):
        await c.answer("Недостаточно монет", show_alert=True)
        return
    if not i["unlimited"]:
        if await ex("UPDATE items SET stock=stock-1 WHERE id=? AND stock>0", (i["id"],)) == 0:
            await add_balance(uid, i["price"])
            await c.answer("Товар закончился", show_alert=True)
            return
    await ex("INSERT INTO purchases(user_id,item_id,name,price,created_at) VALUES(?,?,?,?,?)",
             (uid, i["id"], i["name"], i["price"], today()))
    await c.message.edit_text(f"✅ Вы купили «{esc(i['name'])}» за {i['price']} 🪙. Админы получили уведомление.")
    u = await get_user(uid)
    left = "∞" if i["unlimited"] else (await q_one("SELECT stock FROM items WHERE id=?", (i["id"],)))["stock"]
    await notify_admins(f"🛒 <b>Покупка</b>\n{uname(u)}\nТовар: «{esc(i['name'])}» за {i['price']} 🪙\nОстаток: {left}")
    await c.answer()


# ----- управление магазином (админ) -----
@private.message(F.text == "🛍 Управление магазином", IsAdmin())
async def shop_admin(m: Message, state: FSMContext):
    await state.clear()
    items = await q_all("SELECT * FROM items WHERE active=1 ORDER BY id")
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить товар", callback_data="item:add")
    lines = []
    for i in items:
        stock = "∞" if i["unlimited"] else i["stock"]
        lines.append(f"#{i['id']} <b>{esc(i['name'])}</b> — {i['price']} 🪙, остаток {stock}")
        kb.button(text=f"🗑 #{i['id']} {i['name']}", callback_data=f"item:del:{i['id']}")
    kb.adjust(1)
    await m.answer("🛍 <b>Товары</b>\n\n" + ("\n".join(lines) or "Пока нет товаров."), reply_markup=kb.as_markup())


@private.callback_query(F.data == "item:add", IsAdmin())
async def item_add(c: CallbackQuery, state: FSMContext):
    await state.set_state(ItemAdd.name)
    await c.message.answer("Название товара: (/cancel — отмена)")
    await c.answer()


@private.callback_query(F.data.startswith("item:del:"), IsAdmin())
async def item_del(c: CallbackQuery):
    await ex("UPDATE items SET active=0 WHERE id=?", (int(c.data.split(":")[2]),))
    await c.answer("Товар удалён", show_alert=True)


@private.message(ItemAdd.name)
async def item_name(m: Message, state: FSMContext):
    await state.update_data(name=(m.text or "").strip()[:80])
    await state.set_state(ItemAdd.price)
    await m.answer("Цена (число монет):")


@private.message(ItemAdd.price)
async def item_price(m: Message, state: FSMContext):
    if not (m.text or "").strip().isdigit() or int(m.text) <= 0:
        await m.answer("Введите положительное целое число.")
        return
    await state.update_data(price=int(m.text))
    await state.set_state(ItemAdd.qty)
    await m.answer("Количество (0 = бесконечно):")


@private.message(ItemAdd.qty)
async def item_qty(m: Message, state: FSMContext):
    if not (m.text or "").strip().isdigit():
        await m.answer("Введите целое число (0 = бесконечно).")
        return
    await state.update_data(qty=int(m.text))
    await state.set_state(ItemAdd.desc)
    await m.answer("Описание:")


@private.message(ItemAdd.desc)
async def item_desc(m: Message, state: FSMContext):
    d = await state.get_data()
    await ex("INSERT INTO items(name,price,stock,unlimited,description) VALUES(?,?,?,?,?)",
             (d["name"], d["price"], d["qty"], 1 if d["qty"] == 0 else 0, (m.text or "").strip()))
    await state.clear()
    await m.answer("✅ Товар добавлен.", reply_markup=main_kb(m.from_user.id))


# =====================================================================
#                     УПРАВЛЕНИЕ ЗАДАНИЯМИ (админ)
# =====================================================================
@private.message(F.text == "📝 Управление заданиями", IsAdmin())
async def tasks_admin(m: Message, state: FSMContext):
    await state.clear()
    ts = await q_all("SELECT * FROM tasks WHERE active=1 ORDER BY id")
    n = (await q_one("SELECT COUNT(*) n FROM claims WHERE status='pending'"))["n"]
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить задание", callback_data="ta:add")
    kb.button(text=f"📥 На проверку ({n})", callback_data="ta:pending")
    lines = []
    for t in ts:
        lines.append(f"#{t['id']} {esc(t['title'])} — {t['reward']} 🪙")
        kb.button(text=f"🗑 #{t['id']}", callback_data=f"ta:del:{t['id']}")
    kb.adjust(1, 1, 4)
    await m.answer("📝 <b>Задания</b>\n\n" + ("\n".join(lines) or "Пока нет заданий."), reply_markup=kb.as_markup())


@private.callback_query(F.data == "ta:add", IsAdmin())
async def ta_add(c: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardBuilder()
    for k, (tpl, _) in KINDS.items():
        kb.button(text=tpl.replace("{n}", "N"), callback_data=f"tk:{k}")
    kb.adjust(1)
    await state.set_state(TaskAdd.kind)
    await c.message.answer("Выберите тип задания:", reply_markup=kb.as_markup())
    await c.answer()


@private.callback_query(F.data.startswith("tk:"), TaskAdd.kind)
async def ta_kind(c: CallbackQuery, state: FSMContext):
    kind = c.data.split(":")[1]
    await state.update_data(kind=kind)
    if KINDS[kind][1]:
        await state.set_state(TaskAdd.target)
        await c.message.answer("Введите N (число):")
    else:
        await state.set_state(TaskAdd.title)
        await c.message.answer("Введите название задания:")
    await c.answer()


@private.message(TaskAdd.target)
async def ta_target(m: Message, state: FSMContext):
    if not (m.text or "").strip().isdigit() or int(m.text) <= 0:
        await m.answer("Введите положительное целое число.")
        return
    await state.update_data(target=int(m.text))
    await state.set_state(TaskAdd.reward)
    await m.answer("Награда в монетах:")


@private.message(TaskAdd.title)
async def ta_title(m: Message, state: FSMContext):
    await state.update_data(title=(m.text or "").strip()[:120], target=0)
    await state.set_state(TaskAdd.reward)
    await m.answer("Награда в монетах:")


@private.message(TaskAdd.reward)
async def ta_reward(m: Message, state: FSMContext):
    if not (m.text or "").strip().isdigit() or int(m.text) <= 0:
        await m.answer("Введите положительное целое число.")
        return
    await state.update_data(reward=int(m.text))
    await state.set_state(TaskAdd.desc)
    await m.answer("Описание (или «-», чтобы пропустить):")


@private.message(TaskAdd.desc)
async def ta_desc(m: Message, state: FSMContext):
    d = await state.get_data()
    desc = "" if (m.text or "").strip() == "-" else (m.text or "").strip()
    title = d.get("title") or KINDS[d["kind"]][0].format(n=d["target"])
    await ex("INSERT INTO tasks(kind,title,description,target,reward) VALUES(?,?,?,?,?)",
             (d["kind"], title, desc, d["target"], d["reward"]))
    await state.clear()
    await m.answer(f"✅ Задание добавлено: {esc(title)}", reply_markup=main_kb(m.from_user.id))


@private.callback_query(F.data.startswith("ta:del:"), IsAdmin())
async def ta_del(c: CallbackQuery):
    await ex("UPDATE tasks SET active=0 WHERE id=?", (int(c.data.split(":")[2]),))
    await c.answer("Задание удалено", show_alert=True)


@private.callback_query(F.data == "ta:pending", IsAdmin())
async def ta_pending(c: CallbackQuery):
    rows = await q_all("SELECT c.id cid, t.title, u.* FROM claims c JOIN tasks t ON t.id=c.task_id "
                       "JOIN users u ON u.user_id=c.user_id WHERE c.status='pending' LIMIT 20")
    if not rows:
        await c.answer("Нет заданий на проверке", show_alert=True)
        return
    for r in rows:
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Принять", callback_data=f"mt:ok:{r['cid']}")
        kb.button(text="❌ Отклонить", callback_data=f"mt:no:{r['cid']}")
        await c.message.answer(f"📥 «{esc(r['title'])}»\nОт: {uname(r)}", reply_markup=kb.as_markup())
    await c.answer()


# =====================================================================
#                 МОНЕТЫ / БАН / СТАТИСТИКА / НАСТРОЙКИ / АДМИНЫ
# =====================================================================
@private.message(F.text == "🪙 Монеты", IsAdmin())
async def coins_start(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Coins.target)
    await m.answer("Отправьте ID или @username пользователя: (/cancel — отмена)")


@private.message(Coins.target)
async def coins_target(m: Message, state: FSMContext):
    u = await find_user(m.text)
    if not u:
        await m.answer("Пользователь не найден (он должен был нажать /start). Попробуйте ещё раз.")
        return
    await state.update_data(uid=u["user_id"])
    await state.set_state(Coins.action)
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Выдать", callback_data="cn:give")
    kb.button(text="➖ Забрать", callback_data="cn:take")
    await m.answer(f"{uname(u)}\nБаланс: {u['balance']} 🪙", reply_markup=kb.as_markup())


@private.callback_query(F.data.startswith("cn:"), Coins.action)
async def coins_action(c: CallbackQuery, state: FSMContext):
    await state.update_data(act=c.data.split(":")[1])
    await state.set_state(Coins.amount)
    await c.message.answer("Сколько монет?")
    await c.answer()


@private.message(Coins.amount)
async def coins_amount(m: Message, state: FSMContext):
    if not (m.text or "").strip().isdigit() or int(m.text) <= 0:
        await m.answer("Введите положительное целое число.")
        return
    d = await state.get_data()
    n, uid = int(m.text), d["uid"]
    if d["act"] == "give":
        await add_balance(uid, n)
        note = f"💰 Администратор начислил вам {n} 🪙"
    else:
        u = await get_user(uid)
        n = min(n, u["balance"])
        await add_balance(uid, -n)
        note = f"💸 Администратор списал с вас {n} 🪙"
    await state.clear()
    u = await get_user(uid)
    try:
        await bot.send_message(uid, f"{note}\nБаланс: {u['balance']} 🪙")
    except Exception:
        pass
    await m.answer(f"✅ Готово. Новый баланс: {u['balance']} 🪙", reply_markup=main_kb(m.from_user.id))


@private.message(F.text == "🚫 Бан / Разбан", IsAdmin())
async def ban_start(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(BanS.target)
    await m.answer("Отправьте ID или @username пользователя: (/cancel — отмена)")


@private.message(BanS.target)
async def ban_target(m: Message, state: FSMContext):
    u = await find_user(m.text)
    if not u:
        await m.answer("Пользователь не найден. Попробуйте ещё раз.")
        return
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="🚫 Забанить", callback_data=f"bn:ban:{u['user_id']}")
    kb.button(text="✅ Разбанить", callback_data=f"bn:unban:{u['user_id']}")
    await m.answer(f"{uname(u)}\nСтатус: <b>{u['status']}</b>\nРоль: {esc(u['role']) or '—'}\n"
                   f"Баланс: {u['balance']} 🪙", reply_markup=kb.as_markup())


@private.callback_query(F.data.startswith("bn:"), IsAdmin())
async def ban_action(c: CallbackQuery):
    _, act, uid = c.data.split(":")
    uid = int(uid)
    if is_admin(uid):
        await c.answer("Нельзя банить администратора", show_alert=True)
        return
    if act == "ban":
        await do_ban(uid)
        await c.answer("Забанен (бот + группа)", show_alert=True)
    else:
        await do_unban(uid)
        await c.answer("Разбанен (бот + группа)", show_alert=True)


@private.message(F.text == "📊 Статистика", IsAdmin())
async def stats(m: Message, state: FSMContext):
    await state.clear()
    st = {r["status"]: r["n"] for r in await q_all("SELECT status, COUNT(*) n FROM users GROUP BY status")}
    tot = await q_one("SELECT COALESCE(SUM(balance),0) coins, COALESCE(SUM(msgs_total),0) msgs FROM users")
    td = await q_one("SELECT COALESCE(SUM(cnt),0) s FROM msg_stats WHERE day=?", (today(),))
    wk = await q_one("SELECT COALESCE(SUM(cnt),0) s FROM msg_stats WHERE day>=?", (week_start(),))
    gm = await q_one("SELECT COALESCE(SUM(cnt),0) s FROM game_counts WHERE day=?", (today(),))
    pu = await q_one("SELECT COUNT(*) n, COALESCE(SUM(price),0) s FROM purchases")
    cl = await q_one("SELECT COUNT(*) n FROM claims WHERE status='done'")
    top_msg = await q_all("SELECT u.full_name, u.username, s.cnt FROM msg_stats s JOIN users u ON u.user_id=s.user_id "
                          "WHERE s.day=? ORDER BY s.cnt DESC LIMIT 5", (today(),))
    top_bal = await q_all("SELECT full_name, username, balance FROM users WHERE status='member' "
                          "ORDER BY balance DESC LIMIT 5")

    def nm(r):
        return esc(r["full_name"]) + (f" (@{esc(r['username'])})" if r["username"] else "")

    await m.answer(
        "📊 <b>Статистика</b>\n"
        f"👥 Участников: {st.get('member', 0)} | ждут: {st.get('agreed', 0)} | отказ: {st.get('declined', 0)} | "
        f"бан: {st.get('banned', 0)}\n"
        f"✉️ Сообщений: сегодня {td['s']}, неделя {wk['s']}, всего {tot['msgs']}\n"
        f"💰 Монет в обороте: {tot['coins']}\n"
        f"🎮 Игровых участий сегодня: {gm['s']}\n"
        f"🛒 Покупок: {pu['n']} на {pu['s']} 🪙\n"
        f"✅ Выполнено заданий: {cl['n']}\n\n"
        "🔥 Топ по сообщениям сегодня:\n" + ("\n".join(f"{i}. {nm(r)} — {r['cnt']}" for i, r in enumerate(top_msg, 1)) or "—") +
        "\n\n💎 Топ по балансу:\n" + ("\n".join(f"{i}. {nm(r)} — {r['balance']}" for i, r in enumerate(top_bal, 1)) or "—"))


@private.message(F.text == "⚙️ Настройки", IsAdmin())
async def settings_menu(m: Message, state: FSMContext):
    await state.clear()
    lim = await get_setting("games_per_day", "5")
    kb = InlineKeyboardBuilder()
    kb.button(text="🎮 Изменить лимит игр в группе", callback_data="cfg:limit")
    await m.answer(f"⚙️ <b>Настройки</b>\nИгр в группе в сутки на человека: <b>{'без лимита' if lim == '0' else lim}</b>",
                   reply_markup=kb.as_markup())


@private.callback_query(F.data == "cfg:limit", IsAdmin())
async def cfg_limit(c: CallbackQuery, state: FSMContext):
    await state.set_state(Cfg.limit)
    await c.message.answer("Сколько игр в сутки можно играть в группе? (0 = без лимита)")
    await c.answer()


@private.message(Cfg.limit)
async def cfg_limit_set(m: Message, state: FSMContext):
    if not (m.text or "").strip().isdigit():
        await m.answer("Введите целое число.")
        return
    await set_setting("games_per_day", int(m.text))
    await state.clear()
    await m.answer("✅ Сохранено.", reply_markup=main_kb(m.from_user.id))


@private.message(F.text == "👑 Админы", F.from_user.id == OWNER_ID)
async def admins_menu(m: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить админа", callback_data="adm:add")
    kb.button(text="➖ Удалить админа", callback_data="adm:del")
    kb.adjust(1)
    await m.answer("👑 <b>Админы</b>\n" + ("\n".join(f"• <code>{a}</code>" for a in sorted(ADMINS)) or "Пока нет."),
                   reply_markup=kb.as_markup())


@private.callback_query(F.data.in_({"adm:add", "adm:del"}), F.from_user.id == OWNER_ID)
async def admins_act(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdminS.add if c.data == "adm:add" else AdminS.remove)
    await c.message.answer("Отправьте ID пользователя:")
    await c.answer()


@private.message(AdminS.add, F.from_user.id == OWNER_ID)
async def admins_add(m: Message, state: FSMContext):
    if not (m.text or "").strip().isdigit():
        await m.answer("Нужен числовой ID.")
        return
    uid = int(m.text)
    await ex("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (uid,))
    ADMINS.add(uid)
    await ex("UPDATE users SET status='member' WHERE user_id=? AND status IN ('new','agreed','declined')", (uid,))
    await state.clear()
    await m.answer("✅ Админ добавлен (он должен нажать /start у бота).", reply_markup=main_kb(m.from_user.id))


@private.message(AdminS.remove, F.from_user.id == OWNER_ID)
async def admins_remove(m: Message, state: FSMContext):
    if not (m.text or "").strip().isdigit():
        await m.answer("Нужен числовой ID.")
        return
    await ex("DELETE FROM admins WHERE user_id=?", (int(m.text),))
    ADMINS.discard(int(m.text))
    await state.clear()
    await m.answer("✅ Админ удалён.", reply_markup=main_kb(m.from_user.id))


# =====================================================================
#                      ИГРЫ В ЛС: КАЗИНО И УГАДАЙ ЦИФРУ
# =====================================================================
async def parse_bet(m: Message):
    t = (m.text or "").strip()
    if not t.isdigit() or int(t) <= 0:
        await m.answer("Введите положительное целое число или /cancel.")
        return None
    u = await get_user(m.from_user.id)
    if int(t) > u["balance"]:
        await m.answer(f"Недостаточно монет. Ваш баланс: {u['balance']} 🪙")
        return None
    return int(t)


@private.message(F.text == "🎮 Игры", IsMember())
async def games_menu(m: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="🎰 Казино", callback_data="g:casino")
    kb.button(text="🔢 Угадай цифру", callback_data="g:guess")
    kb.adjust(1)
    await m.answer(
        "🎮 <b>Игры</b>\n\n"
        f"🎰 Казино: три одинаковых — x{CASINO_MULT_TRIPLE}, 777 — x{CASINO_MULT_JACKPOT}\n"
        "🔢 Угадай цифру: 1–3 → x2.7, 1–5 → x4.5, 1–10 → x9\n\n"
        "В группе доступны: /ttt &lt;ставка&gt; (крестики-нолики) и /rps &lt;ставка&gt; (камень-ножницы-бумага).",
        reply_markup=kb.as_markup())


@private.callback_query(F.data == "g:casino", IsMember())
async def g_casino(c: CallbackQuery, state: FSMContext):
    u = await get_user(c.from_user.id)
    await state.set_state(Game.casino)
    await c.message.answer(f"🎰 Введите ставку (баланс {u['balance']} 🪙) или /cancel:")
    await c.answer()


@private.message(Game.casino, IsMember())
async def casino_bet(m: Message, state: FSMContext):
    bet = await parse_bet(m)
    if bet is None:
        return
    if not await take_balance(m.from_user.id, bet):
        await m.answer("Недостаточно монет.")
        return
    await state.clear()
    msg = await m.answer_dice(emoji="🎰")
    v = msg.dice.value
    await asyncio.sleep(3)
    d = v - 1
    a, b, c = d % 4, (d // 4) % 4, d // 16
    mult = (CASINO_MULT_JACKPOT if a == 3 else CASINO_MULT_TRIPLE) if a == b == c else 0
    if mult:
        win = bet * mult
        await add_balance(m.from_user.id, win)
        text = f"🎉 Выигрыш x{mult}! +{win - bet} 🪙"
    else:
        text = f"😢 Не повезло. −{bet} 🪙"
    u = await get_user(m.from_user.id)
    await m.answer(f"{text}\nБаланс: {u['balance']} 🪙\nЕщё раз — «🎮 Игры».", reply_markup=main_kb(m.from_user.id))


@private.callback_query(F.data == "g:guess", IsMember())
async def g_guess(c: CallbackQuery, state: FSMContext):
    u = await get_user(c.from_user.id)
    await state.set_state(Game.guess_bet)
    await c.message.answer(f"🔢 Введите ставку (баланс {u['balance']} 🪙) или /cancel:")
    await c.answer()


@private.message(Game.guess_bet, IsMember())
async def guess_bet(m: Message, state: FSMContext):
    bet = await parse_bet(m)
    if bet is None:
        return
    await state.update_data(bet=bet)
    await state.set_state(Game.guess_diff)
    kb = InlineKeyboardBuilder()
    for n, k in GUESS_COEF.items():
        kb.button(text=f"1–{n}  (x{k})", callback_data=f"gs:{n}")
    kb.adjust(1)
    await m.answer(f"Ставка {bet} 🪙. Выберите диапазон:", reply_markup=kb.as_markup())


@private.callback_query(F.data.startswith("gs:"), Game.guess_diff)
async def guess_diff(c: CallbackQuery, state: FSMContext):
    n = int(c.data.split(":")[1])
    await state.update_data(n=n)
    await state.set_state(Game.guess_pick)
    kb = InlineKeyboardBuilder()
    for i in range(1, n + 1):
        kb.button(text=str(i), callback_data=f"gp:{i}")
    kb.adjust(5)
    await c.message.edit_text(f"Загадано число от 1 до {n}. Ваш выбор?", reply_markup=kb.as_markup())
    await c.answer()


@private.callback_query(F.data.startswith("gp:"), Game.guess_pick)
async def guess_pick(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    bet, n = d["bet"], d["n"]
    if not await take_balance(c.from_user.id, bet):
        await state.clear()
        await c.message.edit_text("Недостаточно монет.")
        await c.answer()
        return
    await state.clear()
    pick, secret = int(c.data.split(":")[1]), random.randint(1, n)
    if pick == secret:
        win = int(bet * GUESS_COEF[n])
        await add_balance(c.from_user.id, win)
        text = f"🎉 Угадали! Было {secret}. +{win - bet} 🪙"
    else:
        text = f"😢 Не угадали. Было {secret}. −{bet} 🪙"
    u = await get_user(c.from_user.id)
    await c.message.edit_text(f"{text}\nБаланс: {u['balance']} 🪙")
    await c.answer()


# запасной обработчик
@private.message(StateFilter(None))
async def fallback(m: Message):
    u = await get_user(m.from_user.id)
    if u["status"] == "agreed":
        await m.answer(f"Ваш код: <code>{u['code']}</code>\n⏳ Ожидайте подтверждения.")
    elif u["status"] == "member":
        await m.answer("Выберите действие в меню 👇", reply_markup=main_kb(m.from_user.id))
    else:
        await m.answer("Отправьте /start.")


# =====================================================================
#                              ГРУППА
# =====================================================================
RPS_E = {"r": "🪨", "s": "✂️", "p": "📄"}
BEATS = {"r": "s", "s": "p", "p": "r"}
LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]
SYM = ["❌", "⭕"]


def ttt_winner(bd):
    for a, b, c in LINES:
        if bd[a] is not None and bd[a] == bd[b] == bd[c]:
            return bd[a]
    return None


def ttt_kb(gid, g):
    kb = InlineKeyboardBuilder()
    for i, v in enumerate(g["board"]):
        kb.button(text=SYM[v] if v is not None else "▫️", callback_data=f"ttt:{gid}:{i}")
    kb.adjust(3)
    return kb.as_markup()


def ttt_board_text(g):
    rows = ["".join(SYM[v] if v is not None else "▫️" for v in g["board"][i:i + 3]) for i in (0, 3, 6)]
    return "\n".join(rows)


def game_head(g):
    t = "Крестики-нолики" if g["type"] == "ttt" else "Камень-ножницы-бумага"
    return f"<b>{t}</b>\n{SYM[0] if g['type']=='ttt' else '👤'} {g['names'][0]}  vs  {SYM[1] if g['type']=='ttt' else '👤'} {g['names'][1]}\nСтавка: {g['bet']} 🪙"


async def game_edit(g, text, kb=None):
    try:
        await bot.edit_message_text(text, chat_id=g["chat_id"], message_id=g["msg_id"], reply_markup=kb)
    except Exception:
        pass


async def finish(gid, winner, reason=""):
    g = games.pop(gid, None)
    if not g:
        return
    g["done"] = True
    bet, p = g["bet"], g["p"]
    await ex("DELETE FROM active_games WHERE id=?", (gid,))
    if winner is None:
        await add_balance(p[0], bet)
        await add_balance(p[1], bet)
        res = f"🤝 Ничья. Ставки возвращены.{reason}"
    else:
        fee = round(bet * COMMISSION)
        await add_balance(p[winner], bet * 2 - fee)
        await ex("UPDATE users SET wins=wins+1 WHERE user_id=?", (p[winner],))
        res = (f"🏆 Победитель: <b>{g['names'][winner]}</b>\n"
               f"Выигрыш: +{bet - fee} 🪙 (ставка соперника {bet} − комиссия {fee}){reason}")
    body = ttt_board_text(g) + "\n" if g["type"] == "ttt" else ""
    await game_edit(g, f"{game_head(g)}\n\n{body}{res}")


async def watchdog(gid):
    while True:
        await asyncio.sleep(10)
        g = games.get(gid)
        if not g:
            return
        if time.time() - g["last"] > GAME_TIMEOUT:
            note = "\n⏱ Время вышло."
            if g["type"] == "ttt":
                await finish(gid, 1 - g["turn"], note)
            elif len(g["picks"]) == 0:
                await finish(gid, None, note)
            else:
                await finish(gid, list(g["picks"])[0], note)
            return


async def expire_challenge(gid):
    await asyncio.sleep(120)
    ch = pending.pop(gid, None)
    if ch:
        try:
            await bot.edit_message_text("⌛ Вызов истёк.", chat_id=ch["chat_id"], message_id=ch["msg_id"])
        except Exception:
            pass


@grp.message(Command("ttt", "rps"))
async def challenge(m: Message, command: CommandObject):
    uid = m.from_user.id
    u = await get_user(uid)
    if not u or u["status"] != "member":
        return
    try:
        bet = int((command.args or "").split()[0])
    except (ValueError, IndexError):
        await m.reply(f"Формат: /{command.command} &lt;ставка&gt;")
        return
    if bet <= 0:
        await m.reply("Ставка должна быть больше 0.")
        return
    if u["balance"] < bet:
        await m.reply(f"Недостаточно монет (баланс {u['balance']} 🪙).")
        return
    if await games_left(uid) <= 0:
        await m.reply("Лимит игр на сегодня исчерпан.")
        return
    gid = uuid.uuid4().hex[:8]
    kb = InlineKeyboardBuilder()
    kb.button(text="⚔️ Принять", callback_data=f"ch:acc:{gid}")
    kb.button(text="✖️ Отмена", callback_data=f"ch:can:{gid}")
    name = "Крестики-нолики" if command.command == "ttt" else "Камень-ножницы-бумага"
    sent = await m.answer(f"⚔️ <b>{esc(m.from_user.full_name)}</b> вызывает на «{name}»!\n"
                          f"Ставка: {bet} 🪙 (комиссия победителю — {int(COMMISSION*100)}% со ставки соперника)",
                          reply_markup=kb.as_markup())
    pending[gid] = dict(type=command.command, p1=uid, name=esc(m.from_user.full_name), bet=bet,
                        chat_id=sent.chat.id, msg_id=sent.message_id)
    spawn(expire_challenge(gid))


@grp.callback_query(F.data.startswith("ch:"))
async def challenge_cb(c: CallbackQuery):
    _, act, gid = c.data.split(":")
    ch = pending.get(gid)
    uid = c.from_user.id
    if not ch:
        await c.answer("Вызов уже недоступен", show_alert=True)
        return
    if act == "can":
        if uid != ch["p1"] and not is_admin(uid):
            await c.answer("Это не ваш вызов", show_alert=True)
            return
        pending.pop(gid, None)
        await c.message.edit_text("Вызов отменён.")
        await c.answer()
        return
    if uid == ch["p1"]:
        await c.answer("Нельзя играть с самим собой", show_alert=True)
        return
    u = await get_user(uid)
    if not u or u["status"] != "member":
        await c.answer("Вы не участник бота", show_alert=True)
        return
    if await games_left(uid) <= 0 or await games_left(ch["p1"]) <= 0:
        await c.answer("Лимит игр на сегодня исчерпан", show_alert=True)
        return
    bet = ch["bet"]
    pending.pop(gid, None)
    if not await take_balance(uid, bet):
        pending[gid] = ch
        await c.answer("Недостаточно монет", show_alert=True)
        return
    if not await take_balance(ch["p1"], bet):
        await add_balance(uid, bet)
        await c.message.edit_text("Вызов отменён: у создателя не хватает монет.")
        await c.answer()
        return
    g = dict(type=ch["type"], p=[ch["p1"], uid], names=[ch["name"], esc(c.from_user.full_name)], bet=bet,
             board=[None] * 9, turn=0, picks={}, last=time.time(), chat_id=c.message.chat.id,
             msg_id=c.message.message_id, done=False)
    games[gid] = g
    await ex("INSERT INTO active_games(id,p1,p2,bet) VALUES(?,?,?,?)", (gid, ch["p1"], uid, bet))
    for x in g["p"]:
        await ex("INSERT INTO game_counts(user_id,day,cnt) VALUES(?,?,1) "
                 "ON CONFLICT(user_id,day) DO UPDATE SET cnt=cnt+1", (x, today()))
    spawn(watchdog(gid))
    if g["type"] == "ttt":
        await c.message.edit_text(f"{game_head(g)}\n\nХод: {SYM[0]} {g['names'][0]}", reply_markup=ttt_kb(gid, g))
    else:
        kb = InlineKeyboardBuilder()
        for k, e in RPS_E.items():
            kb.button(text=e, callback_data=f"rps:{gid}:{k}")
        await c.message.edit_text(f"{game_head(g)}\n\nВыбирайте! (выбор скрыт)", reply_markup=kb.as_markup())
    await c.answer()


@grp.callback_query(F.data.startswith("ttt:"))
async def ttt_move(c: CallbackQuery):
    _, gid, i = c.data.split(":")
    g = games.get(gid)
    if not g or g["done"]:
        await c.answer("Игра завершена", show_alert=True)
        return
    uid = c.from_user.id
    if uid not in g["p"]:
        await c.answer("Вы не участник этой игры", show_alert=True)
        return
    if g["p"][g["turn"]] != uid:
        await c.answer("Сейчас не ваш ход")
        return
    i = int(i)
    if g["board"][i] is not None:
        await c.answer("Клетка занята")
        return
    g["board"][i] = g["turn"]
    g["last"] = time.time()
    w = ttt_winner(g["board"])
    if w is not None:
        await finish(gid, w)
    elif all(v is not None for v in g["board"]):
        await finish(gid, None)
    else:
        g["turn"] ^= 1
        await game_edit(g, f"{game_head(g)}\n\nХод: {SYM[g['turn']]} {g['names'][g['turn']]}", ttt_kb(gid, g))
    await c.answer()


@grp.callback_query(F.data.startswith("rps:"))
async def rps_pick(c: CallbackQuery):
    _, gid, k = c.data.split(":")
    g = games.get(gid)
    if not g or g["done"]:
        await c.answer("Игра завершена", show_alert=True)
        return
    uid = c.from_user.id
    if uid not in g["p"]:
        await c.answer("Вы не участник этой игры", show_alert=True)
        return
    idx = g["p"].index(uid)
    if idx in g["picks"]:
        await c.answer("Вы уже выбрали")
        return
    g["picks"][idx] = k
    g["last"] = time.time()
    await c.answer(f"Вы выбрали {RPS_E[k]}")
    if len(g["picks"]) < 2:
        await game_edit(g, f"{game_head(g)}\n\n✅ {g['names'][idx]} сделал выбор. Ждём соперника…",
                        c.message.reply_markup)
        return
    a, b = g["picks"][0], g["picks"][1]
    shown = f"\n{g['names'][0]}: {RPS_E[a]}   {g['names'][1]}: {RPS_E[b]}"
    winner = None if a == b else (0 if BEATS[a] == b else 1)
    await finish(gid, winner, shown)


@grp.message(Command("bal"))
async def cmd_bal(m: Message):
    u = await get_user(m.from_user.id)
    if u and u["status"] == "member":
        await m.reply(f"💰 Ваш баланс: {u['balance']} 🪙")


@grp.message(Command("top"))
async def cmd_top(m: Message):
    rows = await q_all("SELECT full_name, balance FROM users WHERE status='member' ORDER BY balance DESC LIMIT 10")
    await m.answer("💎 <b>Топ по балансу</b>\n" +
                   "\n".join(f"{i}. {esc(r['full_name'])} — {r['balance']} 🪙" for i, r in enumerate(rows, 1)))


@grp.message()
async def count_message(m: Message):
    if not m.from_user or m.from_user.is_bot:
        return
    if not (m.text or m.caption or m.photo or m.sticker or m.video or m.voice or m.animation
            or m.video_note or m.document or m.audio):
        return
    uid = m.from_user.id
    u = await get_user(uid)
    if not u:
        return
    if u["status"] == "banned" and not is_admin(uid):
        try:
            await m.delete()
            await bot.ban_chat_member(GROUP_ID, uid)
        except Exception:
            pass
        return
    if u["status"] != "member":
        return
    now = time.time()
    if now - last_counted.get(uid, 0) < MSG_COOLDOWN:
        return
    last_counted[uid] = now
    await db.execute("INSERT INTO msg_stats(user_id,day,cnt) VALUES(?,?,1) "
                     "ON CONFLICT(user_id,day) DO UPDATE SET cnt=cnt+1", (uid, today()))
    await db.execute("UPDATE users SET msgs_total=msgs_total+1 WHERE user_id=?", (uid,))
    await db.commit()


# =====================================================================
async def main():
    global bot, db
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    await db.commit()
    ADMINS.update(r["user_id"] for r in await q_all("SELECT user_id FROM admins"))

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    # вернуть ставки из игр, прерванных перезапуском
    stale = await q_all("SELECT * FROM active_games")
    for r in stale:
        await add_balance(r["p1"], r["bet"])
        await add_balance(r["p2"], r["bet"])
    await ex("DELETE FROM active_games")
    if stale:
        try:
            await bot.send_message(GROUP_ID, "♻️ Бот перезапущен, ставки незавершённых игр возвращены.")
        except Exception:
            pass

    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(Guard())
    dp.callback_query.outer_middleware(Guard())
    dp.include_routers(misc, private, grp)
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "chat_join_request", "chat_member"])


if __name__ == "__main__":
    asyncio.run(main())
