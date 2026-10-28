
import asyncio
import hashlib
import hmac
import json
import os
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
import uvicorn

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
APP_URL = os.getenv("APP_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "8000"))
DB_PATH = os.getenv("DB_PATH", "steknft.db")

if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN in environment variables.")

app = FastAPI(title="Fragment Mini App")
app.mount("/static", StaticFiles(directory="static"), name="static")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


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


def verify_init_data(init_data: str):
    if not init_data:
        raise HTTPException(401, "Open this app from the Telegram bot.")

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
        f"{k}={v}" for k, v in sorted(values.items())
    )
    secret_key = hmac.new(
        b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
    ).digest()
    calculated = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
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
            (user_id,)
        )
        row = conn.execute(
            "SELECT * FROM settings WHERE user_id=?", (user_id,)
        ).fetchone()
        return dict(row)


@dp.message(CommandStart())
async def start(message: Message):
    keyboard = None

    if APP_URL.startswith("https://"):
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="Открыть Fragment",
                web_app=WebAppInfo(url=APP_URL)
            )]
        ])

    await message.answer(
        "Привет! Это Fragment.\n\n"
        "Черновики NFT-офферов можно готовить в приложении.\n"
        "Пример команды:\n"
        "!оффер https://t.me/nft/Example 500 Stars\n\n"
        "Важно: оформление сообщения не создаёт настоящий "
        "системный NFT-оффер.",
        reply_markup=keyboard
    )


# Диагностика Business-подключения.
# Эти обработчики ничего не удаляют и не отправляют.
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
        "is_outgoing =", message.from_user.is_bot
        if message.from_user else None,
        flush=True,
    )


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/api/health")
async def health():
    return {"ok": True, "service": "fragment-starter"}


async def current_user(request: Request):
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    return verify_init_data(init_data)


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
        if key in data:
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
        columns = ", ".join(f"{k}=?" for k in clean)
        with db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO settings(user_id) VALUES (?)",
                (user_id,)
            )
            conn.execute(
                f"UPDATE settings SET {columns} WHERE user_id=?",
                (*clean.values(), user_id)
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
        raise HTTPException(400, "Укажи ссылку на подарок и цену.")
    if currency not in ("Stars", "GRAM", "USDT"):
        raise HTTPException(400, "Неизвестная валюта.")
    if len(links) > 5000 or len(price) > 40:
        raise HTTPException(400, "Слишком длинное значение.")

    with db() as conn:
        cur = conn.execute(
            "INSERT INTO drafts(user_id, links, price, currency, created_at) "
            "VALUES (?,?,?,?,?)",
            (user_id, links, price, currency, int(time.time()))
        )
        draft_id = cur.lastrowid

    return {
        "ok": True,
        "draft_id": draft_id,
        "links": links,
        "price": price,
        "currency": currency,
        "note": "Черновик сохранён. Это не отправленный оффер."
    }


@app.get("/api/drafts")
async def list_drafts(request: Request):
    user_id = await current_user(request)

    with db() as conn:
        rows = conn.execute(
            "SELECT id, links, price, currency, created_at FROM drafts "
            "WHERE user_id=? ORDER BY id DESC LIMIT 30",
            (user_id,)
        ).fetchall()

    return {"drafts": [dict(row) for row in rows]}


async def main():
    init_db()

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
    )
    server = uvicorn.Server(config)

    try:
        await asyncio.gather(
            dp.start_polling(
                bot,
                allowed_updates=dp.resolve_used_update_types(),
            ),
            server.serve(),
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
