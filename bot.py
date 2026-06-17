"""
Prediction game bot (play-money only) + Mini App API.

Рынки с виртуальной ликвидностью и капитализацией. Бинарные (Да/Нет) и
мультиисходные (напр. футбол: П1/Ничья/П2). Короткие рынки по курсу (OKX) с
авто-расчётом. Анонимный топ. Пополнение «фантики за кантики». Курс 10 фантиков = $1.

ВАЖНО: игровые фантики. Их нельзя купить и нельзя вывести. Кошелька/крипты/выплат нет.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

import aiohttp
import aiosqlite
from aiohttp import web
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, KeyboardButton, MenuButtonWebApp, Message,
    ReplyKeyboardMarkup, WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
START_BALANCE = int(os.getenv("START_BALANCE", "10000"))
DAILY_BONUS = int(os.getenv("DAILY_BONUS", "500"))
CURRENCY = os.getenv("CURRENCY", "фантики")
FANT_PER_USD = int(os.getenv("FANT_PER_USD", "10"))  # 10 фантиков = $1
SUB_CHANNEL = os.getenv("SUB_CHANNEL", "@testimpolimarpodpivo")
SUB_CHANNEL_URL = os.getenv("SUB_CHANNEL_URL", "https://t.me/testimpolimarpodpivo")
MSK = timezone(timedelta(hours=3))
DB_PATH = os.getenv("DB_PATH", "predict.db")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
API_PORT = int(os.getenv("PORT", os.getenv("API_PORT", "8080")))
WEBAPP_INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp", "index.html")

ASSETS = [("BTC", "BTC-USDT"), ("ETH", "ETH-USDT"), ("TON", "TON-USDT"), ("SOL", "SOL-USDT")]
HORIZONS = [5, 10]
PRICE_SEED = 1500

logging.basicConfig(level=logging.INFO)
router = Router()
db: aiosqlite.Connection
bot_ref: Bot | None = None


async def is_subscribed(user_id: int):
    """True — подписан, False — точно не подписан, None — не смогли проверить (не блокируем)."""
    if not SUB_CHANNEL or bot_ref is None:
        return True
    try:
        m = await bot_ref.get_chat_member(SUB_CHANNEL, user_id)
        return m.status in ("member", "administrator", "creator")
    except Exception as e:
        logging.warning("sub check (бот не админ канала?): %s", e)
        return None


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY, username TEXT, balance INTEGER NOT NULL,
    last_bonus TEXT, streak INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS markets (
    id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'Рынок', status TEXT NOT NULL DEFAULT 'open',
    outcome TEXT, created_by INTEGER NOT NULL, created_at TEXT NOT NULL, resolved_at TEXT,
    kind TEXT NOT NULL DEFAULT 'manual', asset TEXT, target_price REAL, closes_at TEXT,
    seed_yes INTEGER NOT NULL DEFAULT 1000, seed_no INTEGER NOT NULL DEFAULT 1000, options TEXT
);
CREATE TABLE IF NOT EXISTS bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT, market_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    side TEXT NOT NULL, amount INTEGER NOT NULL, created_at TEXT NOT NULL,
    settled INTEGER NOT NULL DEFAULT 0, payout INTEGER NOT NULL DEFAULT 0
);
"""

MIGRATIONS = [
    "ALTER TABLE markets ADD COLUMN category TEXT NOT NULL DEFAULT 'Рынок'",
    "ALTER TABLE markets ADD COLUMN kind TEXT NOT NULL DEFAULT 'manual'",
    "ALTER TABLE markets ADD COLUMN asset TEXT",
    "ALTER TABLE markets ADD COLUMN target_price REAL",
    "ALTER TABLE markets ADD COLUMN closes_at TEXT",
    "ALTER TABLE markets ADD COLUMN seed_yes INTEGER NOT NULL DEFAULT 1000",
    "ALTER TABLE markets ADD COLUMN seed_no INTEGER NOT NULL DEFAULT 1000",
    "ALTER TABLE markets ADD COLUMN options TEXT",
]


def now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now().isoformat()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def anon_name(user_id: int) -> str:
    return "0x" + hashlib.md5(str(user_id).encode()).hexdigest()[:4].upper()


def fmt_price(p: float) -> str:
    return f"{p:,.0f}" if p >= 100 else f"{p:,.2f}"


def usd(fant: int) -> str:
    d = fant / FANT_PER_USD
    if d >= 1_000_000:
        return f"${d/1_000_000:.1f}M"
    if d >= 1000:
        return f"${d/1000:.1f}K"
    return f"${d:.0f}"


async def ensure_user(user_id: int, username: str | None) -> None:
    cur = await db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
    if await cur.fetchone():
        if username:
            await db.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
    else:
        await db.execute("INSERT INTO users (user_id, username, balance, created_at) VALUES (?,?,?,?)",
                         (user_id, username, START_BALANCE, now_iso()))
    await db.commit()


async def get_balance(user_id: int) -> int:
    cur = await db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    row = await cur.fetchone()
    return row["balance"] if row else 0


async def get_rank(user_id: int) -> int:
    cur = await db.execute(
        "SELECT COUNT(*) + 1 AS r FROM users WHERE balance > (SELECT balance FROM users WHERE user_id = ?)", (user_id,)
    )
    return (await cur.fetchone())["r"]


# --------------------------------------------------------------------------- #
#  Рынки: исходы, пулы (бинарные и мультиисходные)
# --------------------------------------------------------------------------- #
async def get_market(market_id: int):
    cur = await db.execute("SELECT * FROM markets WHERE id = ?", (market_id,))
    return await cur.fetchone()


def market_sides(m) -> list[tuple[str, str, int]]:
    """[(side_code, label, seed)]."""
    if m["kind"] == "multi" and m["options"]:
        return [(f"OPT{i}", o["label"], int(o["seed"])) for i, o in enumerate(json.loads(m["options"]))]
    return [("YES", "Да", m["seed_yes"] or 0), ("NO", "Нет", m["seed_no"] or 0)]


async def real_pools(market_id: int) -> dict[str, int]:
    cur = await db.execute("SELECT side, COALESCE(SUM(amount),0) AS s FROM bets WHERE market_id=? GROUP BY side", (market_id,))
    return {r["side"]: r["s"] for r in await cur.fetchall()}


async def pool_map(m) -> dict[str, int]:
    real = await real_pools(m["id"])
    return {side: seed + real.get(side, 0) for side, _, seed in market_sides(m)}


async def user_position(market_id: int, user_id: int) -> dict[str, int]:
    cur = await db.execute(
        "SELECT side, COALESCE(SUM(amount),0) AS s FROM bets WHERE market_id=? AND user_id=? GROUP BY side", (market_id, user_id)
    )
    return {r["side"]: r["s"] for r in await cur.fetchall()}


def pcts(pm: dict[str, int]) -> dict[str, int]:
    total = sum(pm.values()) or 1
    return {s: round(v * 100 / total) for s, v in pm.items()}


# --------------------------------------------------------------------------- #
#  Core actions
# --------------------------------------------------------------------------- #
async def do_place_bet(user_id: int, market_id: int, side: str, amount: int) -> tuple[bool, str]:
    if amount <= 0:
        return False, "Ставка должна быть больше нуля."
    if amount > await get_balance(user_id):
        return False, "Недостаточно фантиков."
    m = await get_market(market_id)
    if not m:
        return False, "Рынок не найден."
    if side not in [s for s, _, _ in market_sides(m)]:
        return False, "Неизвестный исход."
    if m["status"] != "open":
        return False, "Рынок закрыт."
    if m["closes_at"] and datetime.fromisoformat(m["closes_at"]) <= now():
        return False, "Время вышло."
    await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, user_id))
    await db.execute("INSERT INTO bets (market_id, user_id, side, amount, created_at) VALUES (?,?,?,?,?)",
                     (market_id, user_id, side, amount, now_iso()))
    await db.commit()
    return True, "ok"


def bonus_day(ts: datetime):
    """День бонуса со сбросом в 06:00 МСК."""
    return (ts.astimezone(MSK) - timedelta(hours=6)).date()


def next_reset() -> datetime:
    msk = now().astimezone(MSK)
    today6 = msk.replace(hour=6, minute=0, second=0, microsecond=0)
    if msk >= today6:
        today6 += timedelta(days=1)
    return today6.astimezone(timezone.utc)


async def do_bonus(user_id: int) -> tuple[bool, str, int]:
    cur = await db.execute("SELECT last_bonus, streak FROM users WHERE user_id = ?", (user_id,))
    r = await cur.fetchone()
    streak = (r["streak"] or 0) if r else 0
    today = bonus_day(now())
    if r and r["last_bonus"]:
        last_day = bonus_day(datetime.fromisoformat(r["last_bonus"]))
        if last_day == today:
            left = next_reset() - now()
            h, mm = divmod(int(left.total_seconds()) // 60, 60)
            return False, f"Бонус сегодня уже взят. Следующий в 06:00 МСК (через {h}ч {mm}м).", 0
        streak = streak + 1 if (today - last_day).days == 1 else 1
    else:
        streak = 1
    bonus = DAILY_BONUS + (streak - 1) * 100
    await db.execute("UPDATE users SET balance = balance + ?, last_bonus = ?, streak = ? WHERE user_id = ?",
                     (bonus, now_iso(), streak, user_id))
    await db.commit()
    return True, f"+{bonus} {CURRENCY} (стрик {streak})", bonus


async def do_topup(user_id: int, amount: int) -> tuple[bool, str, int]:
    bal = await get_balance(user_id)
    if amount <= 0:
        return False, "Сумма должна быть больше нуля.", bal
    if amount > 1_000_000:
        return False, "Максимум 1 000 000 за раз.", bal
    await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    await db.commit()
    return True, f"+{amount} {CURRENCY} (кантики ∞)", await get_balance(user_id)


async def resolve_uid(target: str) -> int | None:
    t = target.lstrip("@")
    if t.isdigit():
        return int(t)
    cur = await db.execute("SELECT user_id FROM users WHERE lower(username)=lower(?)", (t,))
    row = await cur.fetchone()
    return row["user_id"] if row else None


async def admin_give(target: str, amount: int) -> tuple[bool, str, int | None]:
    uid = await resolve_uid(target)
    if uid is None:
        return False, f"Юзер {target} не найден.", None
    cur = await db.execute("SELECT 1 FROM users WHERE user_id=?", (uid,))
    if not await cur.fetchone():
        return False, f"Юзер {uid} ещё не запускал бота (нет в базе).", None
    await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, uid))
    await db.commit()
    bal = await get_balance(uid)
    return True, f"Готово: {anon_name(uid)} (id {uid}) {'+' if amount >= 0 else ''}{amount} {CURRENCY}. Баланс: {bal}.", uid


async def resolve_market(market_id: int, outcome: str) -> dict[int, list[int]]:
    """outcome: код исхода (YES/NO/OPTi) или CANCEL. Возвращает {uid: [staked, payout]}."""
    m = await get_market(market_id)
    cur = await db.execute("SELECT id, user_id, side, amount FROM bets WHERE market_id=? AND settled=0", (market_id,))
    bets = await cur.fetchall()
    res: dict[int, list[int]] = {}
    for b in bets:
        res.setdefault(b["user_id"], [0, 0])[0] += b["amount"]

    if outcome == "CANCEL":
        for b in bets:
            res[b["user_id"]][1] += b["amount"]
            await db.execute("UPDATE bets SET settled=1, payout=? WHERE id=?", (b["amount"], b["id"]))
        await db.execute("UPDATE markets SET status='cancelled', resolved_at=? WHERE id=?", (now_iso(), market_id))
    else:
        pm = await pool_map(m)
        total = sum(pm.values())
        win_pool = pm.get(outcome, 0)
        for b in bets:
            pay = (b["amount"] * total // win_pool) if (b["side"] == outcome and win_pool > 0) else 0
            if pay:
                res[b["user_id"]][1] += pay
            await db.execute("UPDATE bets SET settled=1, payout=? WHERE id=?", (pay, b["id"]))
        await db.execute("UPDATE markets SET status='resolved', outcome=?, resolved_at=? WHERE id=?",
                         (outcome, now_iso(), market_id))
    for uid, (st, pay) in res.items():
        if pay:
            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (pay, uid))
    await db.commit()
    return res


def result_msg(question: str, label: str, staked: int, payout: int, prefix: str = "") -> str:
    head = prefix + "\n" if prefix else ""
    if payout > staked:
        return f"{head}✅ <b>Ставка сыграла!</b>\n«{question}»\nИсход: <b>{label}</b>\nСтавил {staked} → <b>+{payout} {CURRENCY}</b> 🟢"
    if payout > 0:
        return f"{head}↩️ <b>Возврат</b>\n«{question}»\nВернулось {payout} {CURRENCY}."
    return f"{head}❌ <b>Не сыграла</b>\n«{question}»\nИсход: <b>{label}</b>\n−{staked} {CURRENCY}"


async def notify_results(bot: Bot, res: dict[int, list[int]], question: str, label: str, prefix: str = "") -> None:
    for uid, (st, pay) in res.items():
        try:
            await bot.send_message(uid, result_msg(question, label, st, pay, prefix))
        except Exception:
            pass


# --------------------------------------------------------------------------- #
#  Market maker
# --------------------------------------------------------------------------- #
async def get_price(inst: str) -> float | None:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://www.okx.com/api/v5/market/ticker?instId={inst}",
                             timeout=aiohttp.ClientTimeout(total=8)) as r:
                return float((await r.json())["data"][0]["last"])
    except Exception:
        return None


async def ensure_short_term_markets() -> None:
    for asset, inst in ASSETS:
        cur = await db.execute("SELECT COUNT(*) AS c FROM markets WHERE kind='price' AND status='open' AND asset=?", (asset,))
        have = (await cur.fetchone())["c"]
        i = 0
        while have < len(HORIZONS):
            price = await get_price(inst)
            if price is None:
                break
            horizon = HORIZONS[i % len(HORIZONS)]
            i += 1
            await db.execute(
                "INSERT INTO markets (question, category, created_by, created_at, kind, asset, target_price, closes_at, seed_yes, seed_no)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"{asset} будет выше ${fmt_price(price)} через {horizon} мин?", "Курс ⚡", 0, now_iso(),
                 "price", asset, price, (now() + timedelta(minutes=horizon)).isoformat(), PRICE_SEED, PRICE_SEED),
            )
            have += 1
        await db.commit()


async def resolve_expired_price_markets(bot: Bot) -> None:
    cur = await db.execute(
        "SELECT id, asset, target_price, question FROM markets WHERE kind='price' AND status='open' AND closes_at <= ?", (now_iso(),)
    )
    for m in await cur.fetchall():
        inst = next((s for a, s in ASSETS if a == m["asset"]), None)
        price = await get_price(inst) if inst else None
        if price is None:
            continue
        outcome = "YES" if price >= m["target_price"] else "NO"
        res = await resolve_market(m["id"], outcome)
        await notify_results(bot, res, m["question"], "ДА" if outcome == "YES" else "НЕТ",
                             prefix=f"⚡ Курс закрылся на ${fmt_price(price)}")


async def market_maker(bot: Bot) -> None:
    while True:
        try:
            await resolve_expired_price_markets(bot)
            await ensure_short_term_markets()
        except Exception as e:
            logging.warning("market_maker: %s", e)
        await asyncio.sleep(25)


# --------------------------------------------------------------------------- #
#  Рендер (чат)
# --------------------------------------------------------------------------- #
def bar(p_yes: int) -> str:
    f = round(p_yes / 10)
    return "▰" * f + "▱" * (10 - f)


async def market_card(market_id: int, user_id: int | None = None) -> str:
    m = await get_market(market_id)
    if not m:
        return "Рынок не найден."
    pm = await pool_map(m)
    pp = pcts(pm)
    sides = market_sides(m)
    total = sum(pm.values())
    lines = [f"<b>{m['question']}</b>", "", f"💰 Объём: {usd(total)}", ""]
    pos = await user_position(market_id, user_id) if user_id is not None else {}
    for code, label, _ in sides:
        mine = pos.get(code, 0)
        extra = f"  · ты: {mine}" if mine else ""
        lines.append(f"{label} — <b>{pp.get(code,0)}%</b>{extra}")
    if m["status"] == "resolved":
        win = next((l for c, l, _ in sides if c == m["outcome"]), m["outcome"])
        lines += ["", f"Итог: <b>{win}</b>"]
    elif m["status"] != "open":
        lines += ["", "закрыт"]
    return "\n".join(lines)


def market_kb(m, status: str):
    kb = InlineKeyboardBuilder()
    if status == "open":
        for code, label, _ in market_sides(m):
            kb.button(text=label, callback_data=f"bet:{m['id']}:{code}")
    kb.button(text="↻ Обновить", callback_data=f"mkt:{m['id']}")
    kb.button(text="« Назад", callback_data="list")
    n = len(market_sides(m))
    kb.adjust(n if status == "open" else 1, 2)
    return kb.as_markup()


async def categories_kb():
    cur = await db.execute("SELECT category, COUNT(*) AS c FROM markets WHERE status='open' GROUP BY category ORDER BY c DESC")
    rows = await cur.fetchall()
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=f"{r['category']} · {r['c']}", callback_data=f"cat:{r['category']}")
    kb.adjust(2)
    return rows, kb.as_markup()


async def category_markets_kb(cat: str):
    cur = await db.execute(
        "SELECT id, question FROM markets WHERE status='open' AND category=? ORDER BY kind='price' DESC, id DESC LIMIT 25", (cat,)
    )
    rows = await cur.fetchall()
    kb = InlineKeyboardBuilder()
    for r in rows:
        t = (r["question"][:42] + "…") if len(r["question"]) > 43 else r["question"]
        kb.button(text=t, callback_data=f"mkt:{r['id']}")
    kb.button(text="« Категории", callback_data="list")
    kb.adjust(1)
    return rows, kb.as_markup()


class BetStates(StatesGroup):
    amount = State()


async def safe_edit(cq: CallbackQuery, text: str, reply_markup=None) -> None:
    try:
        await cq.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest:
        pass


def main_kb() -> ReplyKeyboardMarkup:
    rows = []
    if WEBAPP_URL:
        rows.append([KeyboardButton(text="🚀 Открыть приложение", web_app=WebAppInfo(url=WEBAPP_URL))])
    rows += [
        [KeyboardButton(text="📊 Рынки"), KeyboardButton(text="👤 Профиль")],
        [KeyboardButton(text="💎 Пополнить"), KeyboardButton(text="🎁 Бонус")],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def topup_kb():
    kb = InlineKeyboardBuilder()
    for a in (1000, 5000, 25000, 100000):
        kb.button(text=f"+{a:,}".replace(",", " "), callback_data=f"top:{a}")
    kb.adjust(2)
    return kb.as_markup()


def app_inline_kb():
    kb = InlineKeyboardBuilder()
    if WEBAPP_URL:
        kb.button(text="📈 Открыть приложение", web_app=WebAppInfo(url=WEBAPP_URL))
    kb.button(text="📊 Рынки в чате", callback_data="list")
    kb.adjust(1)
    return kb.as_markup()


def sub_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Подписаться на канал", url=SUB_CHANNEL_URL)
    kb.button(text="✅ Я подписался", callback_data="checksub")
    kb.adjust(1)
    return kb.as_markup()


SUB_PROMPT = ("🔒 <b>Доступ только для подписчиков</b>\n\nПодпишись на наш канал, "
              "потом нажми «Я подписался».")


@router.callback_query(F.data == "checksub")
async def cb_checksub(cq: CallbackQuery):
    if await is_subscribed(cq.from_user.id) is False:
        await cq.answer("Пока не вижу подписки 🙃 Подпишись и попробуй снова.", show_alert=True)
        return
    await cq.answer("Готово! Доступ открыт ✅")
    await safe_edit(cq, "Спасибо за подписку! Доступ открыт. Жми /start или кнопки снизу 👇")


# --------------------------------------------------------------------------- #
#  Хэндлеры — пользователь
# --------------------------------------------------------------------------- #
@router.message(CommandStart())
async def cmd_start(msg: Message):
    await ensure_user(msg.from_user.id, msg.from_user.username or msg.from_user.full_name)
    if await is_subscribed(msg.from_user.id) is False:
        await msg.answer(SUB_PROMPT, reply_markup=sub_kb())
        return
    bal = await get_balance(msg.from_user.id)
    text = (f"<b>Polymarket</b> · рынки прогнозов\n\nБаланс: <b>{bal}</b> {CURRENCY} ≈ {usd(bal)}\n\n"
            "Лучше всего играть в приложении 👇 Кнопки управления — снизу.")
    if is_admin(msg.from_user.id):
        text += "\n\n<i>admin:</i> /new · /close · /resolve · /give id|@user сумма"
    await msg.answer(text, reply_markup=main_kb())
    await msg.answer("Открыть:", reply_markup=app_inline_kb())


@router.message(Command("help"))
async def cmd_help(msg: Message):
    await cmd_start(msg)


@router.message(Command("app"))
async def cmd_app(msg: Message):
    await msg.answer("Открыть приложение 👇", reply_markup=app_inline_kb())


@router.message(Command("balance"))
async def cmd_balance(msg: Message):
    await ensure_user(msg.from_user.id, msg.from_user.username or msg.from_user.full_name)
    bal = await get_balance(msg.from_user.id)
    await msg.answer(f"💰 Баланс: <b>{bal}</b> {CURRENCY} ≈ {usd(bal)}")


@router.message(Command("bonus"))
@router.message(F.text == "🎁 Бонус")
async def cmd_bonus(msg: Message):
    await ensure_user(msg.from_user.id, msg.from_user.username or msg.from_user.full_name)
    ok, text, _ = await do_bonus(msg.from_user.id)
    await msg.answer(f"{'🎁 ' if ok else '⏳ '}{text}\nБаланс: <b>{await get_balance(msg.from_user.id)}</b>")


@router.message(Command("topup"))
@router.message(F.text == "💎 Пополнить")
async def cmd_topup(msg: Message):
    await ensure_user(msg.from_user.id, msg.from_user.username or msg.from_user.full_name)
    await msg.answer("💎 <b>Фантики за кантики</b>\nКантиков у тебя: <b>∞</b> (тест).\nСколько добавить?", reply_markup=topup_kb())


@router.callback_query(F.data.startswith("top:"))
async def cb_topup(cq: CallbackQuery):
    await ensure_user(cq.from_user.id, cq.from_user.username or cq.from_user.full_name)
    ok, info, bal = await do_topup(cq.from_user.id, int(cq.data.split(":")[1]))
    await cq.answer(info if ok else "Не вышло")
    await safe_edit(cq, f"✅ {info}\nБаланс: <b>{bal}</b> {CURRENCY} ≈ {usd(bal)}\n\nЕщё?", reply_markup=topup_kb())


@router.message(Command("me"))
@router.message(F.text == "👤 Профиль")
async def cmd_me(msg: Message):
    await ensure_user(msg.from_user.id, msg.from_user.username or msg.from_user.full_name)
    uid = msg.from_user.id
    bal = await get_balance(uid)
    cur = await db.execute("SELECT COUNT(*) AS c FROM bets WHERE user_id=?", (uid,))
    bets = (await cur.fetchone())["c"]
    cur = await db.execute("SELECT COUNT(*) AS w FROM bets WHERE user_id=? AND settled=1 AND payout>amount", (uid,))
    wins = (await cur.fetchone())["w"]
    await msg.answer(
        f"👤 <b>Профиль</b>\nБаланс: <b>{bal}</b> {CURRENCY} ≈ {usd(bal)}\n"
        f"Место: <b>#{await get_rank(uid)}</b> · тег <code>{anon_name(uid)}</code>\n"
        f"Ставок: {bets} · угадано: {wins}", reply_markup=app_inline_kb()
    )


@router.message(Command("top"))
async def cmd_top(msg: Message):
    cur = await db.execute("SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT 10")
    rows = await cur.fetchall()
    medals = ["🥇", "🥈", "🥉"] + ["▫️"] * 7
    lines = ["🏆 <b>Топ игроков</b>", ""]
    for i, r in enumerate(rows):
        tag = "ты" if r["user_id"] == msg.from_user.id else anon_name(r["user_id"])
        lines.append(f"{medals[i]} <code>{tag}</code> — <b>{r['balance']}</b> ({usd(r['balance'])})")
    await msg.answer("\n".join(lines) if rows else "Пусто.")


@router.message(Command("markets"))
@router.message(F.text == "📊 Рынки")
async def cmd_markets(msg: Message):
    await ensure_user(msg.from_user.id, msg.from_user.username or msg.from_user.full_name)
    rows, kb = await categories_kb()
    await msg.answer("📂 <b>Категории рынков</b> — выбери:" if rows else "Рынков нет.", reply_markup=kb if rows else None)


@router.callback_query(F.data == "list")
async def cb_list(cq: CallbackQuery):
    rows, kb = await categories_kb()
    await safe_edit(cq, "📂 <b>Категории рынков</b> — выбери:" if rows else "Рынков нет.", reply_markup=kb if rows else None)
    await cq.answer()


@router.callback_query(F.data.startswith("cat:"))
async def cb_cat(cq: CallbackQuery):
    cat = cq.data.split(":", 1)[1]
    rows, kb = await category_markets_kb(cat)
    await safe_edit(cq, f"📂 <b>{cat}</b> — выбери рынок:" if rows else "Пусто.", reply_markup=kb)
    await cq.answer()


@router.callback_query(F.data.startswith("mkt:"))
async def cb_market(cq: CallbackQuery):
    market_id = int(cq.data.split(":")[1])
    m = await get_market(market_id)
    if not m:
        await cq.answer("Нет рынка", show_alert=True)
        return
    await safe_edit(cq, await market_card(market_id, cq.from_user.id), reply_markup=market_kb(m, m["status"]))
    await cq.answer()


@router.callback_query(F.data.startswith("bet:"))
async def cb_bet_start(cq: CallbackQuery, state: FSMContext):
    _, mid, side = cq.data.split(":")
    m = await get_market(int(mid))
    if not m or m["status"] != "open":
        await cq.answer("Закрыт", show_alert=True)
        return
    label = next((l for c, l, _ in market_sides(m) if c == side), side)
    await ensure_user(cq.from_user.id, cq.from_user.username or cq.from_user.full_name)
    await state.update_data(market_id=int(mid), side=side)
    await state.set_state(BetStates.amount)
    await cq.message.answer(f"Ставка на <b>{label}</b>. Баланс {await get_balance(cq.from_user.id)}.\nСколько? Пришли число (или /cancel).")
    await cq.answer()


@router.message(Command("cancel"))
async def cmd_cancel(msg: Message, state: FSMContext):
    if await state.get_state() is not None:
        await state.clear()
        await msg.answer("Ок.", reply_markup=main_kb())


@router.message(BetStates.amount)
async def cb_bet_amount(msg: Message, state: FSMContext):
    if not (msg.text or "").strip().isdigit():
        await msg.answer("Нужно число (или /cancel).")
        return
    data = await state.get_data()
    ok, info = await do_place_bet(msg.from_user.id, data["market_id"], data["side"], int(msg.text))
    if not ok:
        await msg.answer(info)
        return
    await state.clear()
    await msg.answer(f"✓ Ставка принята. Баланс: <b>{await get_balance(msg.from_user.id)}</b>")
    m = await get_market(data["market_id"])
    await msg.answer(await market_card(data["market_id"], msg.from_user.id), reply_markup=market_kb(m, "open"))


# --------------------------------------------------------------------------- #
#  Хэндлеры — админ
# --------------------------------------------------------------------------- #
@router.message(Command("give"))
async def cmd_give(msg: Message, bot: Bot):
    if not is_admin(msg.from_user.id):
        return
    parts = (msg.text or "").split()
    if len(parts) != 3 or not parts[2].lstrip("-").isdigit():
        await msg.answer("Использование: <code>/give 123456789 5000</code> или <code>/give @username 5000</code>")
        return
    amount = int(parts[2])
    ok, info, uid = await admin_give(parts[1], amount)
    await msg.answer(("✅ " if ok else "⚠️ ") + info)
    if ok and uid:
        try:
            bal = await get_balance(uid)
            if amount >= 0:
                await bot.send_message(uid, f"💎 Администратор начислил тебе <b>+{amount}</b> {CURRENCY}!\nБаланс: <b>{bal}</b> ≈ {usd(bal)}")
            else:
                await bot.send_message(uid, f"➖ Администратор списал <b>{-amount}</b> {CURRENCY}.\nБаланс: <b>{bal}</b>")
        except Exception:
            pass


@router.message(Command("msg"))
async def cmd_msg(msg: Message, bot: Bot):
    if not is_admin(msg.from_user.id):
        return
    parts = (msg.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await msg.answer("Использование: <code>/msg @username Привет!</code> или <code>/msg 123456789 текст</code>")
        return
    uid = await resolve_uid(parts[1])
    if uid is None:
        await msg.answer("Юзер не найден.")
        return
    try:
        await bot.send_message(uid, parts[2])
        await msg.answer("✅ Отправлено.")
    except Exception as e:
        await msg.answer(f"⚠️ Не доставлено: {e}")


@router.message(Command("broadcast"))
async def cmd_broadcast(msg: Message, bot: Bot):
    if not is_admin(msg.from_user.id):
        return
    text = msg.text.partition(" ")[2].strip()
    if not text:
        await msg.answer("Использование: <code>/broadcast Текст рассылки всем юзерам</code>")
        return
    cur = await db.execute("SELECT user_id FROM users")
    uids = [r["user_id"] for r in await cur.fetchall()]
    sent = 0
    for uid in uids:
        try:
            await bot.send_message(uid, text)
            sent += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await msg.answer(f"📣 Разослано: {sent}/{len(uids)}")


@router.message(Command("new"))
async def cmd_new(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    q = msg.text.partition(" ")[2].strip()
    if not q:
        await msg.answer("Использование: <code>/new Биткоин выше $80k к 1 июля?</code>")
        return
    cur = await db.execute("INSERT INTO markets (question, created_by, created_at) VALUES (?,?,?)", (q, msg.from_user.id, now_iso()))
    await db.commit()
    await msg.answer(f"Рынок #{cur.lastrowid} создан.")


@router.message(Command("close"))
async def cmd_close(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    arg = msg.text.partition(" ")[2].strip()
    if not arg.isdigit():
        await msg.answer("Использование: <code>/close 3</code>")
        return
    cur = await db.execute("UPDATE markets SET status='closed' WHERE id=? AND status='open'", (int(arg),))
    await db.commit()
    await msg.answer("Закрыт." if cur.rowcount else "Не найден.")


@router.message(Command("resolve"))
async def cmd_resolve(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    cur = await db.execute("SELECT id, question FROM markets WHERE status IN ('open','closed') AND kind!='price' ORDER BY id DESC LIMIT 40")
    rows = await cur.fetchall()
    if not rows:
        await msg.answer("Нет рынков для расчёта.")
        return
    kb = InlineKeyboardBuilder()
    for r in rows:
        t = (r["question"][:40] + "…") if len(r["question"]) > 41 else r["question"]
        kb.button(text=t, callback_data=f"rsv:{r['id']}")
    kb.adjust(1)
    await msg.answer("Что закрываем?", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("rsv:"))
async def cb_resolve_pick(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("Только админ", show_alert=True)
        return
    mid = int(cq.data.split(":")[1])
    m = await get_market(mid)
    kb = InlineKeyboardBuilder()
    for code, label, _ in market_sides(m):
        kb.button(text=f"Победа: {label}", callback_data=f"rout:{mid}:{code}")
    kb.button(text="↩️ Отмена (возврат)", callback_data=f"rout:{mid}:CANCEL")
    kb.adjust(1)
    await safe_edit(cq, f"Исход рынка #{mid}?", reply_markup=kb.as_markup())
    await cq.answer()


@router.callback_query(F.data.startswith("rout:"))
async def cb_resolve_do(cq: CallbackQuery, bot: Bot):
    if not is_admin(cq.from_user.id):
        await cq.answer("Только админ", show_alert=True)
        return
    _, mid, outcome = cq.data.split(":")
    m = await get_market(int(mid))
    if not m or m["status"] not in ("open", "closed"):
        await cq.answer("Уже закрыт", show_alert=True)
        return
    label = "ОТМЕНА" if outcome == "CANCEL" else next((l for c, l, _ in market_sides(m) if c == outcome), outcome)
    res = await resolve_market(int(mid), outcome)
    await safe_edit(cq, f"Рынок #{mid} закрыт: <b>{label}</b>. Участников: {len(res)}.")
    await cq.answer("Готово")
    await notify_results(bot, res, m["question"], label)


# --------------------------------------------------------------------------- #
#  Mini App API
# --------------------------------------------------------------------------- #
def validate_init_data(init_data: str) -> dict | None:
    if not init_data:
        return None
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received = parsed.pop("hash", None)
    if not received:
        return None
    dcs = "\n".join(f"{k}={parsed[k]}" for k in sorted(parsed))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest(), received):
        return None
    try:
        return json.loads(parsed.get("user", ""))
    except (ValueError, TypeError):
        return None


async def api_auth(request: web.Request) -> tuple[dict | None, dict]:
    try:
        body = await request.json()
    except Exception:
        body = {}
    user = validate_init_data(body.get("initData") or request.headers.get("X-Init-Data", ""))
    if user:
        await ensure_user(int(user["id"]), user.get("username") or user.get("first_name"))
    return user, body


async def market_json(market_id: int, user_id: int | None = None) -> dict:
    m = await get_market(market_id)
    pm = await pool_map(m)
    pp = pcts(pm)
    total = sum(pm.values())
    pos = await user_position(market_id, user_id) if user_id is not None else {}
    sides = market_sides(m)
    out = {"id": m["id"], "question": m["question"], "cat": m["category"], "status": m["status"],
           "kind": m["kind"], "closes_at": m["closes_at"], "pool": total, "vol_usd": usd(total),
           "multi": m["kind"] == "multi",
           "options": [{"side": c, "label": l, "p": pp.get(c, 0), "pool": pm.get(c, 0), "pos": pos.get(c, 0)}
                       for c, l, _ in sides]}
    if m["kind"] != "multi":
        out.update({"yes": pm.get("YES", 0), "no": pm.get("NO", 0),
                    "p_yes": pp.get("YES", 0), "p_no": pp.get("NO", 0),
                    "pos": {"yes": pos.get("YES", 0), "no": pos.get("NO", 0)}})
    return out


async def api_state(request: web.Request) -> web.Response:
    user, _ = await api_auth(request)
    if not user:
        return web.json_response({"ok": False, "error": "auth"}, status=401)
    uid = int(user["id"])
    cur = await db.execute("SELECT id FROM markets WHERE status IN ('open','closed') ORDER BY kind='price' DESC, id DESC LIMIT 90")
    markets = [await market_json(r["id"], uid) for r in await cur.fetchall()]
    cur = await db.execute("SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT 15")
    top = [{"name": anon_name(r["user_id"]), "balance": r["balance"], "usd": usd(r["balance"]), "me": r["user_id"] == uid}
           for r in await cur.fetchall()]
    cur = await db.execute("SELECT COUNT(*) AS c FROM bets WHERE user_id=?", (uid,))
    bets = (await cur.fetchone())["c"]
    cur = await db.execute("SELECT COUNT(*) AS w FROM bets WHERE user_id=? AND settled=1 AND payout>amount", (uid,))
    wins = (await cur.fetchone())["w"]
    cur = await db.execute(
        "SELECT b.side, b.amount, b.payout, b.settled, m.question FROM bets b JOIN markets m ON m.id=b.market_id "
        "WHERE b.user_id=? ORDER BY b.id DESC LIMIT 25", (uid,)
    )
    history = [{"q": r["question"], "amount": r["amount"], "payout": r["payout"],
                "settled": bool(r["settled"]), "won": bool(r["settled"]) and r["payout"] > r["amount"]}
               for r in await cur.fetchall()]
    bal = await get_balance(uid)
    sub = await is_subscribed(uid)
    return web.json_response({
        "ok": True,
        "me": {"balance": bal, "usd": usd(bal), "rank": await get_rank(uid), "currency": CURRENCY,
               "tag": anon_name(uid), "bets": bets, "wins": wins, "history": history,
               "subscribed": sub is not False, "sub_url": SUB_CHANNEL_URL},
        "markets": markets, "leaderboard": top,
    })


async def api_bet(request: web.Request) -> web.Response:
    user, body = await api_auth(request)
    if not user:
        return web.json_response({"ok": False, "error": "auth"}, status=401)
    if await is_subscribed(int(user["id"])) is False:
        return web.json_response({"ok": False, "error": "Подпишись на канал, чтобы делать ставки."})
    try:
        mid, side, amount = int(body["market_id"]), str(body["side"]), int(body["amount"])
    except (KeyError, ValueError, TypeError):
        return web.json_response({"ok": False, "error": "bad_params"}, status=400)
    ok, info = await do_place_bet(int(user["id"]), mid, side, amount)
    return web.json_response({"ok": ok, "error": None if ok else info, "balance": await get_balance(int(user["id"]))})


async def api_bonus(request: web.Request) -> web.Response:
    user, _ = await api_auth(request)
    if not user:
        return web.json_response({"ok": False, "error": "auth"}, status=401)
    ok, info, amount = await do_bonus(int(user["id"]))
    return web.json_response({"ok": ok, "message": info, "amount": amount, "balance": await get_balance(int(user["id"]))})


async def api_topup(request: web.Request) -> web.Response:
    user, body = await api_auth(request)
    if not user:
        return web.json_response({"ok": False, "error": "auth"}, status=401)
    try:
        amount = int(body["amount"])
    except (KeyError, ValueError, TypeError):
        return web.json_response({"ok": False, "error": "bad_params"}, status=400)
    ok, info, bal = await do_topup(int(user["id"]), amount)
    return web.json_response({"ok": ok, "message": info, "error": None if ok else info, "balance": bal})


@web.middleware
async def cors_mw(request: web.Request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as exc:
            resp = exc
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Init-Data"
    return resp


def build_api() -> web.Application:
    app = web.Application(middlewares=[cors_mw])
    app.router.add_post("/api/state", api_state)
    app.router.add_post("/api/bet", api_bet)
    app.router.add_post("/api/bonus", api_bonus)
    app.router.add_post("/api/topup", api_topup)
    app.router.add_route("OPTIONS", "/{tail:.*}", lambda r: web.Response(status=204))
    app.router.add_get("/", lambda r: web.FileResponse(WEBAPP_INDEX))
    return app


async def main():
    global db
    if not BOT_TOKEN:
        raise SystemExit("Нет BOT_TOKEN.")
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    for sql in MIGRATIONS:
        try:
            await db.execute(sql)
        except Exception:
            pass
    await db.commit()

    # Авто-сидинг рынков, если база пустая (диск на бесплатных хостингах
    # эфемерный и стирается при каждом перезапуске — поэтому сеем при старте).
    cur = await db.execute("SELECT COUNT(*) FROM markets")
    (n_markets,) = await cur.fetchone()
    if n_markets == 0:
        base = os.path.dirname(os.path.abspath(__file__))
        for fname in ("seed.sql", "multi_seed.sql"):
            path = os.path.join(base, fname)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    await db.executescript(f.read())
        await db.commit()
        logging.info("Seeded markets from SQL files")

    global bot_ref
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    bot_ref = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    runner = web.AppRunner(build_api())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", API_PORT).start()
    logging.info("API on :%s", API_PORT)

    await bot.delete_webhook(drop_pending_updates=True)
    if WEBAPP_URL:
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="📈 Открыть", web_app=WebAppInfo(url=WEBAPP_URL)))
        except Exception as e:
            logging.warning("menu button: %s", e)

    asyncio.create_task(market_maker(bot))
    logging.info("Bot started. Admins: %s", ADMIN_IDS or "—")
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
