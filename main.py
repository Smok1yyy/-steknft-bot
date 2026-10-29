
import asyncio
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from urllib.parse import parse_qsl, urlparse

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    WebAppInfo,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn


# ---------- Environment ----------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
APP_URL = os.getenv("APP_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "8000"))
DB_PATH = os.getenv("DB_PATH", "steknft.db")

if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN in environment variables.")

app = FastAPI(title="StekNFT")
app.mount("/static", StaticFiles(directory="static"), name="static")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


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

        conn.execute("""
            CREATE TABLE IF NOT EXISTS offers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id TEXT NOT NULL,
                nft_link TEXT NOT NULL,
                nft_name TEXT NOT NULL,
                price TEXT NOT NULL,
                currency TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                message_id INTEGER,
                created_at INTEGER NOT NULL
            )
        """)


# ---------- Telegram Mini App authentication ----------

def verify_init_data(init_data: str) -> int:
    if not init_data:
        raise HTTPException(
            401,
            "Открой Mini App через Telegram-бота."
        )

    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", None)

    if not received_hash:
        raise HTTPException(401, "Нет подписи Telegram.")

    try:
        auth_date = int(values.get("auth_date", "0"))
    except ValueError:
        raise HTTPException(401, "Некорректная дата авторизации.")

    if abs(int(time.time()) - auth_date) > 86400:
        raise HTTPException(401, "Сессия Telegram истекла.")

    data_check_string = "\n".join(
        f"{key}={value}"
        for key, value in sorted(values.items())
    )

    secret_key = hmac.new(
        b"WebAppData",
        BOT_TOKEN.encode(),
        hashlib.sha256,
    ).digest()

    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(401, "Неверная подпись Telegram.")

    try:
        user = json.loads(values["user"])
        return int(user["id"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        raise HTTPException(401, "Не удалось определить пользователя.")


async def current_user(request: Request) -> int:
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    return verify_init_data(init_data)


# ---------- Settings ----------

def get_settings(user_id: int) -> dict:
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


# ---------- NFT helpers ----------

def validate_nft_link(link: str) -> bool:
    try:
        parsed = urlparse(link)
    except ValueError:
        return False

    return (
        parsed.scheme == "https"
        and parsed.netloc.lower() == "t.me"
        and parsed.path.startswith("/nft/")
        and bool(parsed.path.removeprefix("/nft/").strip("/"))
        and not parsed.query
        and not parsed.fragment
    )


def get_nft_name(link: str) -> str:
    """
    Extract a readable name from a Telegram collectible URL.
    This does not verify ownership or query an official NFT catalogue.
    """
    path = urlparse(link).path.rstrip("/")
    slug = path.rsplit("/", 1)[-1]

    match = re.fullmatch(r"(.+)-(\d+)", slug)

    if match:
        return f"{match.group(1)} #{match.group(2)}"

    return slug or "NFT по ссылке"


# ---------- Keyboards ----------

def offer_keyboard(offer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"offer:reject:{offer_id}",
                ),
                InlineKeyboardButton(
                    text="✅ Принять",
                    callback_data=f"offer:accept:{offer_id}",
                ),
            ]
        ]
    )


def accepted_keyboard(
    offer_id: int,
    nft_link: str,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎁 Открыть NFT",
                    url=nft_link,
                )
            ],
            [
                InlineKeyboardButton(
                    text="📋 Инструкция передачи NFT",
                    callback_data=f"offer:info:{offer_id}",
                )
            ],
        ]
    )


# ---------- Bot commands ----------

@dp.message(CommandStart())
async def start(message: Message):
    keyboard = None

    if APP_URL.startswith("https://"):
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Открыть StekNFT",
                        web_app=WebAppInfo(url=APP_URL),
                    )
                ]
            ]
        )

    await message.answer(
        "Привет! Это StekNFT.\n\n"
        "Создавай NFT-предложения через Mini App.\n"
        "Офферы отправляются в чат, указанный в настройках.",
        reply_markup=keyboard,
    )


# ---------- Offer callbacks ----------

@dp.callback_query(F.data.startswith("offer:"))
async def handle_offer_button(callback: CallbackQuery):
    if not callback.data or not callback.message:
        await callback.answer()
        return

    parts = callback.data.split(":")

    if len(parts) != 3:
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return

    _, action, raw_id = parts

    try:
        offer_id = int(raw_id)
    except ValueError:
        await callback.answer("Оффер не найден.", show_alert=True)
        return

    with db() as conn:
        offer = conn.execute(
            "SELECT * FROM offers WHERE id = ?",
            (offer_id,),
        ).fetchone()

    if not offer:
        await callback.answer("Оффер не найден.", show_alert=True)
        return

    # Only the recipient chat where this offer was sent
    # can process its buttons.
    if str(callback.message.chat.id) != str(offer["chat_id"]):
        await callback.answer("Эта кнопка предназначена другому чату.", show_alert=True)
        return

    if action == "info":
        await callback.answer(
            "Открой ссылку NFT и проверь условия сделки. "
            "Не передавай NFT до подтверждения оплаты.",
            show_alert=True,
        )
        return

    if action not in ("accept", "reject"):
        await callback.answer("Неизвестное действие.", show_alert=True)
        return

    new_status = "accepted" if action == "accept" else "rejected"

    with db() as conn:
        cur = conn.execute(
            """
            UPDATE offers
            SET status = ?
            WHERE id = ? AND status = 'pending'
            """,
            (new_status, offer_id),
        )
        changed = cur.rowcount

    if changed != 1:
        await callback.answer(
            "Этот оффер уже обработан.",
            show_alert=True,
        )
        return

    if action == "accept":
        text = (
            "🎁 StekNFT — предложение принято\n\n"
            f"💎 NFT: {offer['nft_name']}\n"
            f"💰 Цена: {offer['price']} {offer['currency']}\n\n"
            "Проверь NFT и условия сделки перед передачей."
        )

        await callback.message.edit_text(
            text,
            reply_markup=accepted_keyboard(
                offer_id,
                offer["nft_link"],
            ),
            link_preview_options={"is_disabled": False},
        )
        await callback.answer("Предложение принято.")

    else:
        await callback.message.edit_text(
            "❌ Предложение StekNFT отклонено.\n\n"
            f"💎 NFT: {offer['nft_name']}\n"
            f"💰 Цена: {offer['price']} {offer['currency']}"
        )
        await callback.answer("Предложение отклонено.")


# ---------- Web app ----------

@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/api/health")
async def health():
    return {"ok": True, "service": "StekNFT"}


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
        raise HTTPException(400, "Неизвестный язык.")

    if clean.get("text_style") not in (None, "default", "original"):
        raise HTTPException(400, "Неизвестный стиль.")

    if clean.get("buttons_layout") not in (None, "side", "stacked"):
        raise HTTPException(400, "Неизвестное расположение кнопок.")

    if clean.get("image_position") not in (None, "above", "below"):
        raise HTTPException(400, "Неизвестное расположение изображения.")

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

    nft_link = str(data.get("links", "")).strip()
    price = str(data.get("price", "")).strip()
    currency = str(data.get("currency", "Stars")).strip()

    if not nft_link or not price:
        raise HTTPException(400, "Укажи ссылку на NFT и цену.")

    if currency not in ("Stars", "GRAM", "USDT"):
        raise HTTPException(400, "Неизвестная валюта.")

    if len(nft_link) > 500 or len(price) > 40:
        raise HTTPException(400, "Слишком длинное значение.")

    if not validate_nft_link(nft_link):
        raise HTTPException(
            400,
            "Укажи ссылку вида https://t.me/nft/Название-12345",
        )

    settings = get_settings(user_id)
    receiver = str(settings.get("receiver", "")).strip()

    if not receiver:
        raise HTTPException(
            400,
            "Сначала укажи получателя в настройках StekNFT.",
        )

    nft_name = get_nft_name(nft_link)

    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO offers
            (user_id, chat_id, nft_link, nft_name, price,
             currency, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                user_id,
                receiver,
                nft_link,
                nft_name,
                price,
                currency,
                int(time.time()),
            ),
        )
        offer_id = cur.lastrowid

    message_text = (
        "🎁 StekNFT — предложение NFT\n\n"
        f"💎 NFT: {nft_name}\n"
        f"🔗 Ссылка: {nft_link}\n"
        f"💰 Цена: {price} {currency}\n\n"
        "Предложение создано через StekNFT."
    )

    try:
        sent = await bot.send_message(
            chat_id=receiver,
            text=message_text,
            reply_markup=offer_keyboard(offer_id),
            link_preview_options={"is_disabled": False},
        )
    except Exception as exc:
        print(
            "Offer send error:",
            type(exc).__name__,
            str(exc),
            flush=True,
        )

        with db() as conn:
            conn.execute(
                "UPDATE offers SET status = 'send_failed' WHERE id = ?",
                (offer_id,),
            )

        raise HTTPException(
            502,
            "Не удалось отправить оффер. Проверь получателя "
            "и доступ бота к чату.",
        )

    with db() as conn:
        conn.execute(
            "UPDATE offers SET message_id = ? WHERE id = ?",
            (sent.message_id, offer_id),
        )

        # Keep a copy in the drafts history as well.
        conn.execute(
            """
            INSERT INTO drafts
            (user_id, links, price, currency, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, nft_link, price, currency, int(time.time())),
        )

    return {
        "ok": True,
        "offer_id": offer_id,
        "nft_name": nft_name,
        "sent": True,
        "message": "Оффер отправлен ботом StekNFT.",
    }


@app.get("/api/drafts")
async def list_drafts(request: Request):
    user_id = await current_user(request)

    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, links, price, currency, created_at
            FROM drafts
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 30
            """,
            (user_id,),
        ).fetchall()

    return {"drafts": [dict(row) for row in rows]}


@app.get("/api/offers")
async def list_offers(request: Request):
    user_id = await current_user(request)

    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, nft_link, nft_name, price, currency,
                   status, created_at
            FROM offers
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 30
            """,
            (user_id,),
        ).fetchall()

    return {"offers": [dict(row) for row in rows]}


# ---------- Startup ----------

async def main():
    init_db()

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

    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
