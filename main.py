
import asyncio
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    WebAppInfo,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BusinessConnection,
)
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from telethon import TelegramClient, events
from telethon.sessions import StringSession
import uvicorn


# ---------- Environment ----------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
APP_URL = os.getenv("APP_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "8000"))
DB_PATH = os.getenv("DB_PATH", "steknft.db")

API_ID_RAW = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN in environment variables.")

app = FastAPI(title="Fragment Mini App")
app.mount("/static", StaticFiles(directory="static"), name="static")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

user_client = None


# ---------- Database ----------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                user_id INTEGER PRIMARY KEY,
                receiver TEXT DEFAULT '',
                language TEXT DEFAULT 'RU',
                text_style TEXT DEFAULT 'original',
                bold INTEGER DEFAULT 1,
                buttons_layout TEXT DEFAULT 'side',
                accept_text TEXT DEFAULT 'Принять',
                reject_text TEXT DEFAULT 'Отклонить',
                icons INTEGER DEFAULT 0,
                preview INTEGER DEFAULT 1,
                image_position TEXT DEFAULT 'above'
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                links TEXT NOT NULL,
                price TEXT NOT NULL,
                currency TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)


# ---------- Telegram Mini App validation ----------

def verify_init_data(init_data: str):
    if not init_data:
        raise HTTPException(
            401, "Open this app from the Telegram bot."
        )

    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", None)

    if not received_hash:
        raise HTTPException(401, "Missing Telegram signature.")

    try:
        auth_date = int(values.get("auth_date", "0"))
    except ValueError:
        raise HTTPException(401, "Invalid Telegram auth date.")

    if abs(int(time.time()) - auth_date) > 86400:
        raise HTTPException(401, "Telegram session expired.")

    data_check_string = "\n".join(
        f"{key}={value}"
        for key, value in sorted(values.items())
    )

    secret_key = hmac.new(
        b"WebAppData",
        BOT_TOKEN.encode(),
        hashlib.sha256,
    ).digest()

    calculated = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated, received_hash):
        raise HTTPException(401, "Invalid Telegram signature.")

    try:
        user = json.loads(values["user"])
        return int(user["id"])
    except Exception:
        raise HTTPException(401, "Could not read Telegram user.")


def get_settings(user_id):
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO settings(user_id) VALUES (?)",
            (user_id,),
        )
        row = conn.execute(
            "SELECT * FROM settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return dict(row)


async def current_user(request: Request):
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    return verify_init_data(init_data)


# ---------- Bot ----------

@dp.message(CommandStart())
async def start(message: Message):
    keyboard = None

    if APP_URL.startswith("https://"):
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Открыть Fragment",
                        web_app=WebAppInfo(url=APP_URL),
                    )
                ]
            ]
        )

    await message.answer(
        "Привет! Это Fragment.\n\n"
        "Пример команды для личного аккаунта:\n"
        "!оффер https://t.me/nft/Example 500 Stars\n\n"
        "Команда обрабатывается только при работающем "
        "подключении личного аккаунта.",
        reply_markup=keyboard,
    )


# ---------- Telegram Business diagnostics ----------

@dp.business_connection()
async def on_business_connection(connection: BusinessConnection):
    print(
        "BUSINESS CONNECTION:",
        "id =", connection.id,
        "enabled =", connection.is_enabled,
        "rights =", connection.rights,
        flush=True,
    )


@dp.business_message()
async def on_business_message(message: Message):
    print(
        "BUSINESS MESSAGE:",
        "chat_id =", message.chat.id,
        "message_id =", message.message_id,
        "from_user_id =",
        message.from_user.id if message.from_user else None,
        "text =", repr(message.text),
        "connection_id =", message.business_connection_id,
        flush=True,
    )


# ---------- Personal-account offer handler ----------

OFFER_PATTERN = re.compile(
    r"!оффер\s+(https?://\S+)\s+(.+)",
    flags=re.IGNORECASE | re.DOTALL,
)


async def handle_offer(event):
    # Process only messages sent by this account in private chats.
    if not event.is_private or not event.out:
        return

    text = (event.raw_text or "").strip()
    match = OFFER_PATTERN.fullmatch(text)

    if not match:
        return

    nft_link = match.group(1).strip()
    price = match.group(2).strip()

    if len(nft_link) > 500 or len(price) > 100:
        print("Offer ignored: input too long.", flush=True)
        return

    formatted = (
        "🎁 NFT OFFER\n\n"
        f"🔗 NFT: {nft_link}\n"
        f"💰 Цена: {price}"
    )

    try:
        # Send the replacement first so the original isn't lost
        # if Telegram rejects the outgoing message.
        await user_client.send_message(
            event.chat_id,
            formatted,
            link_preview=False,
        )

        await event.delete()

        print(
            "Offer replaced successfully:",
            "chat_id =", event.chat_id,
            flush=True,
        )

    except Exception as exc:
        # The original command is not deliberately deleted if
        # sending the replacement fails.
        print(
            "Offer processing error:",
            type(exc).__name__,
            str(exc),
            flush=True,
        )


# ---------- Web app ----------

@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/api/health")
async def health():
    return {"ok": True, "service": "fragment-starter"}


@app.get("/api/settings")
async def read_settings(request: Request):
    user_id = await current_user(request)
    return get_settings(user_id)


@app.post("/api/settings")
async def save_settings(request: Request):
    user_id = await current_user(request)
    data = await request.json()

    allowed = {
        "receiver": str,
        "language": str,
        "text_style": str,
        "bold": bool,
        "buttons_layout": str,
        "accept_text": str,
        "reject_text": str,
        "icons": bool,
        "preview": bool,
        "image_position": str,
    }

    clean = {}

    for key, typ in allowed.items():
        if key not in data:
            continue

        value = data[key]

        if typ is bool:
            clean[key] = int(bool(value))
        elif isinstance(value, str):
            clean[key] = value[:500]

    if clean.get("language") not in (None, "RU", "EN"):
        raise HTTPException(400, "Unsupported language")

    if clean.get("text_style") not in (None, "default", "original"):
        raise HTTPException(400, "Unsupported text style")

    if clean.get("buttons_layout") not in (None, "side", "stacked"):
        raise HTTPException(400, "Unsupported button layout")

    if clean.get("image_position") not in (None, "above", "below"):
        raise HTTPException(400, "Unsupported image position")

    if clean:
        columns = ", ".join(f"{key}=?" for key in clean)

        with db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO settings(user_id) VALUES (?)",
                (user_id,),
            )
            conn.execute(
                f"UPDATE settings SET {columns} WHERE user_id=?",
                (*clean.values(), user_id),
            )

    return get_settings(user_id)


@app.post("/api/drafts")
async def create_draft(request: Request):
    user_id = await current_user(request)
    data = await request.json()

    links = str(data.get("links", "")).strip()
    price = str(data.get("price", "")).strip()
    currency = str(data.get("currency", "Stars")).strip()

    if not links or not price:
        raise HTTPException(
            400, "Укажи ссылку на подарок и цену."
        )

    if currency not in ("Stars", "GRAM", "USDT"):
        raise HTTPException(400, "Неизвестная валюта.")

    if len(links) > 5000 or len(price) > 40:
        raise HTTPException(400, "Слишком длинное значение.")

    with db() as conn:
        cur = conn.execute(
            "INSERT INTO drafts"
            "(user_id, links, price, currency, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, links, price, currency, int(time.time())),
        )
        draft_id = cur.lastrowid

    return {
        "ok": True,
        "draft_id": draft_id,
        "links": links,
        "price": price,
        "currency": currency,
        "note": "Черновик сохранён. Это не отправленный оффер.",
    }


@app.get("/api/drafts")
async def list_drafts(request: Request):
    user_id = await current_user(request)

    with db() as conn:
        rows = conn.execute(
            "SELECT id, links, price, currency, created_at "
            "FROM drafts WHERE user_id=? "
            "ORDER BY id DESC LIMIT 30",
            (user_id,),
        ).fetchall()

    return {"drafts": [dict(row) for row in rows]}


# ---------- Startup ----------

async def main():
    global user_client

    init_db()

    if API_ID_RAW and API_HASH and SESSION_STRING:
        try:
            api_id = int(API_ID_RAW)
        except ValueError:
            raise RuntimeError("API_ID must be an integer.")

        user_client = TelegramClient(
            StringSession(SESSION_STRING),
            api_id,
            API_HASH,
        )

        await user_client.connect()

        if not await user_client.is_user_authorized():
            await user_client.disconnect()
            raise RuntimeError(
                "SESSION_STRING is not authorized. "
                "Create a new session and update Render."
            )

        user_client.add_event_handler(
            handle_offer,
            events.NewMessage(outgoing=True, incoming=False),
        )

        print(
            "Personal Telegram account connected. "
            "Offer handler is active.",
            flush=True,
        )
    else:
        print(
            "WARNING: Personal-account automation is disabled. "
            "Set API_ID, API_HASH and SESSION_STRING in Render.",
            flush=True,
        )

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
    )
    server = uvicorn.Server(config)

    tasks = [
        asyncio.create_task(
            dp.start_polling(
                bot,
                allowed_updates=dp.resolve_used_update_types(),
            )
        ),
        asyncio.create_task(server.serve()),
    ]

    if user_client is not None:
        tasks.append(
            asyncio.create_task(user_client.run_until_disconnected())
        )

    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)

        if user_client is not None:
            await user_client.disconnect()

        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
