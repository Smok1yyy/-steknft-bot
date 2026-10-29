
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
from telethon import TelegramClient, events
from telethon.sessions import StringSession
import uvicorn


# ---------- Configuration ----------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
APP_URL = os.getenv("APP_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "8000"))

# Keep the existing database path to preserve current data.
DB_PATH = os.getenv("DB_PATH", "steknft.db")

API_ID = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN in environment variables.")

app = FastAPI(title="Fragment")
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


# ---------- Mini App authentication ----------

def verify_init_data(init_data: str) -> int:
    if not init_data:
        raise HTTPException(
            status_code=401,
            detail="Открой Mini App через Telegram-бота.",
        )

    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", None)

    if not received_hash:
        raise HTTPException(
            status_code=401,
            detail="Нет подписи Telegram.",
        )

    try:
        auth_date = int(values.get("auth_date", "0"))
    except ValueError:
        raise HTTPException(
            status_code=401,
            detail="Некорректная дата авторизации.",
        )

    if abs(int(time.time()) - auth_date) > 86400:
        raise HTTPException(
            status_code=401,
            detail="Сессия Telegram истекла.",
        )

    check_string = "\n".join(
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
        check_string.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(
            status_code=401,
            detail="Неверная подпись Telegram.",
        )

    try:
        user = json.loads(values["user"])
        return int(user["id"])
    except (
        KeyError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ):
        raise HTTPException(
            status_code=401,
            detail="Не удалось определить пользователя.",
        )


async def current_user(request: Request) -> int:
    return verify_init_data(
        request.headers.get("X-Telegram-Init-Data", "")
    )


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
    slug = urlparse(link).path.rstrip("/").rsplit("/", 1)[-1]
    match = re.fullmatch(r"(.+)-(\d+)", slug)

    if match:
        return f"{match.group(1)} #{match.group(2)}"

    return slug or "NFT"


def validate_price(price: str) -> bool:
    if not price or len(price) > 40:
        return False

    return bool(
        re.fullmatch(r"\d+(?:[.,]\d+)?", price)
        and float(price.replace(",", ".")) > 0
    )


# ---------- Keyboards ----------

def offer_keyboard(offer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="❌ Отклонить",
                callback_data=f"offer:reject:{offer_id}",
            ),
            InlineKeyboardButton(
                text="✅ Принять",
                callback_data=f"offer:accept:{offer_id}",
            ),
        ]]
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


# ---------- Sending offers ----------

async def send_offer(
    chat_id,
    nft_link,
    price,
    currency,
    user_id=0,
):
    if not validate_nft_link(nft_link):
        raise ValueError("Некорректная ссылка на NFT.")

    if not validate_price(price):
        raise ValueError("Укажи корректную положительную цену.")

    if currency not in ("Stars", "TON", "GRAM", "USDT"):
        raise ValueError("Неизвестная валюта.")

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
                int(user_id or 0),
                str(chat_id),
                nft_link,
                nft_name,
                price,
                currency,
                int(time.time()),
            ),
        )
        offer_id = cur.lastrowid

    text = (
        "🎁 Fragment — предложение NFT\n\n"
        f"💎 NFT: {nft_name}\n"
        f"🔗 Ссылка: {nft_link}\n"
        f"💰 Цена: {price} {currency}\n\n"
        "Предложение создано через Fragment."
    )

    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=offer_keyboard(offer_id),
            link_preview_options={"is_disabled": False},
        )
    except Exception:
        with db() as conn:
            conn.execute(
                "UPDATE offers SET status = 'send_failed' WHERE id = ?",
                (offer_id,),
            )
        raise

    with db() as conn:
        conn.execute(
            "UPDATE offers SET message_id = ? WHERE id = ?",
            (sent.message_id, offer_id),
        )

    return offer_id, sent, nft_name


# ---------- Bot commands ----------

@dp.message(CommandStart())
async def start(message: Message):
    keyboard = None

    if APP_URL.startswith("https://"):
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text="Открыть Fragment",
                    web_app=WebAppInfo(url=APP_URL),
                )
            ]]
        )

    await message.answer(
        "Привет! Это Fragment.\n\n"
        "Создавай NFT-предложения через Mini App.",
        reply_markup=keyboard,
    )


# ---------- Offer buttons ----------

@dp.callback_query(F.data.startswith("offer:"))
async def handle_offer_button(callback: CallbackQuery):
    if not callback.data or not callback.message:
        await callback.answer()
        return

    parts = callback.data.split(":")

    if len(parts) != 3:
        await callback.answer(
            "Некорректная кнопка.",
            show_alert=True,
        )
        return

    _, action, raw_id = parts

    try:
        offer_id = int(raw_id)
    except ValueError:
        await callback.answer(
            "Оффер не найден.",
            show_alert=True,
        )
        return

    with db() as conn:
        offer = conn.execute(
            "SELECT * FROM offers WHERE id = ?",
            (offer_id,),
        ).fetchone()

    if not offer:
        await callback.answer(
            "Оффер не найден.",
            show_alert=True,
        )
        return

    if str(callback.message.chat.id) != str(offer["chat_id"]):
        await callback.answer(
            "Эта кнопка предназначена другому чату.",
            show_alert=True,
        )
        return

    if action == "info":
        await callback.answer(
            "Проверь NFT и условия сделки. Не передавай NFT "
            "до подтверждения оплаты.",
            show_alert=True,
        )
        return

    if action not in ("accept", "reject"):
        await callback.answer(
            "Неизвестное действие.",
            show_alert=True,
        )
        return

    status = "accepted" if action == "accept" else "rejected"

    with db() as conn:
        cur = conn.execute(
            """
            UPDATE offers
            SET status = ?
            WHERE id = ? AND status = 'pending'
            """,
            (status, offer_id),
        )
        changed = cur.rowcount

    if changed != 1:
        await callback.answer(
            "Оффер уже обработан.",
            show_alert=True,
        )
        return

    if action == "accept":
        await callback.message.edit_text(
            "🎁 Fragment — предложение принято\n\n"
            f"💎 NFT: {offer['nft_name']}\n"
            f"💰 Цена: {offer['price']} {offer['currency']}\n\n"
            "Проверь NFT и условия сделки перед передачей.",
            reply_markup=accepted_keyboard(
                offer_id,
                offer["nft_link"],
            ),
            link_preview_options={"is_disabled": False},
        )

        await callback.answer("Предложение принято.")

    else:
        await callback.message.edit_text(
            "❌ Предложение Fragment отклонено.\n\n"
            f"💎 NFT: {offer['nft_name']}\n"
            f"💰 Цена: {offer['price']} {offer['currency']}",
        )

        await callback.answer("Предложение отклонено.")


# ---------- Web app ----------

@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/api/health")
async def health():
    return {"ok": True, "service": "Fragment"}


@app.get("/api/settings")
async def read_settings(request: Request):
    return get_settings(await current_user(request))


@app.post("/api/settings")
async def save_settings(request: Request):
    user_id = await current_user(request)

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Некорректный JSON.")

    if not isinstance(data, dict):
        raise HTTPException(400, "Ожидался объект настроек.")

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
            if not isinstance(value, bool):
                raise HTTPException(
                    400,
                    f"Настройка {key} должна быть логическим значением.",
                )
            clean[key] = int(value)

        elif isinstance(value, str):
            clean[key] = value[:500]

        else:
            raise HTTPException(
                400,
                f"Некорректное значение настройки {key}.",
            )

    if clean.get("language") not in (None, "RU", "EN"):
        raise HTTPException(400, "Неизвестный язык.")

    if clean.get("text_style") not in (None, "default", "original"):
        raise HTTPException(400, "Неизвестный стиль.")

    if clean.get("buttons_layout") not in (None, "side", "stacked"):
        raise HTTPException(400, "Неизвестное расположение кнопок.")

    if clean.get("image_position") not in (None, "above", "below"):
        raise HTTPException(400, "Неизвестное расположение изображения.")

    receiver = clean.get("receiver")

    if receiver and not re.fullmatch(
        r"@?[A-Za-z0-9_]{5,32}|-100\d{5,}",
        receiver,
    ):
        raise HTTPException(
            400,
            "Получатель должен быть @username или числовым chat ID.",
        )

    if clean:
        columns = ", ".join(f"{key} = ?" for key in clean)

        with db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO settings(user_id) VALUES (?)",
                (user_id,),
            )

            conn.execute(
                f"UPDATE settings SET {columns} WHERE user_id = ?",
                (*clean.values(), user_id),
            )

    return get_settings(user_id)


@app.post("/api/drafts")
async def create_draft(request: Request):
    user_id = await current_user(request)

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Некорректный JSON.")

    if not isinstance(data, dict):
        raise HTTPException(400, "Ожидались данные оффера.")

    nft_link = str(data.get("links", "")).strip()
    price = str(data.get("price", "")).strip()
    currency = str(data.get("currency", "Stars")).strip()

    if not nft_link or not price:
        raise HTTPException(
            400,
            "Укажи ссылку на NFT и цену.",
        )

    if len(nft_link) > 500 or len(price) > 40:
        raise HTTPException(400, "Слишком длинное значение.")

    if not validate_price(price):
        raise HTTPException(400, "Укажи корректную положительную цену.")

    if currency not in ("Stars", "TON", "GRAM", "USDT"):
        raise HTTPException(400, "Неизвестная валюта.")

    if not validate_nft_link(nft_link):
        raise HTTPException(
            400,
            "Укажи ссылку вида https://t.me/nft/Название-12345",
        )

    receiver = str(
        get_settings(user_id).get("receiver", "")
    ).strip()

    if not receiver:
        raise HTTPException(
            400,
            "Сначала укажи получателя в настройках Fragment.",
        )

    try:
        offer_id, _, nft_name = await send_offer(
            receiver,
            nft_link,
            price,
            currency,
            user_id,
        )

    except Exception as exc:
        print(
            "Offer send error:",
            type(exc).__name__,
            str(exc),
            flush=True,
        )

        raise HTTPException(
            502,
            "Бот не смог отправить сообщение. Получатель должен "
            "сначала открыть бота и нажать Start; для группы "
            "добавь бота в группу.",
        )

    with db() as conn:
        conn.execute(
            """
            INSERT INTO drafts(user_id, links, price, currency, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                user_id,
                nft_link,
                price,
                currency,
                int(time.time()),
            ),
        )

    return {
        "ok": True,
        "offer_id": offer_id,
        "nft_name": nft_name,
        "sent": True,
        "message": "Сообщение с предложением отправлено ботом Fragment.",
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


# ---------- Telethon personal-account command ----------

OFFER_COMMAND_RE = re.compile(
    r"^\s*!оффер\s+(https://t\.me/nft/[^\s]+)\s+"
    r"([0-9]+(?:[.,][0-9]+)?)\s+(Stars|TON|GRAM|USDT)\s*$",
    re.IGNORECASE,
)


async def setup_telethon():
    global user_client

    if not (API_ID and API_HASH and SESSION_STRING):
        print(
            "Telethon disabled: set API_ID, API_HASH and SESSION_STRING.",
            flush=True,
        )
        return

    user_client = TelegramClient(
        StringSession(SESSION_STRING),
        int(API_ID),
        API_HASH,
    )

    await user_client.start()

    async def handle_outgoing_offer(event):
        if not event.out or not event.raw_text:
            return

        match = OFFER_COMMAND_RE.fullmatch(event.raw_text.strip())

        if not match:
            return

        nft_link, price, currency = match.groups()

        currency = next(
            item
            for item in ("Stars", "TON", "GRAM", "USDT")
            if item.lower() == currency.lower()
        )

        price = price.replace(",", ".")

        me = await user_client.get_me()

        if event.chat_id == me.id:
            await event.respond(
                "Напиши команду в чате с получателем, а не в Избранном. "
                "Получатель должен заранее нажать Start у бота Fragment."
            )
            return

        try:
            await send_offer(
                chat_id=event.chat_id,
                nft_link=nft_link,
                price=price,
                currency=currency,
                user_id=me.id,
            )

        except Exception as exc:
            print(
                "Telethon offer error:",
                type(exc).__name__,
                str(exc),
                flush=True,
            )

            await event.respond(
                "❌ Бот не смог отправить сообщение. Проверь, что "
                "получатель нажал Start у бота и бот имеет доступ "
                "к чату. Команда оставлена."
            )
            return

        try:
            await event.delete()
        except Exception as exc:
            print(
                "Could not delete command:",
                str(exc),
                flush=True,
            )

    user_client.add_event_handler(
        handle_outgoing_offer,
        events.NewMessage(outgoing=True),
    )

    print("Fragment Telethon listener started.", flush=True)


# ---------- Startup ----------

async def main():
    init_db()
    await setup_telethon()

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="0.0.0.0",
            port=PORT,
            log_level="info",
        )
    )

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
            asyncio.create_task(
                user_client.run_until_disconnected()
            )
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
