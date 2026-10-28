
import os
import asyncio
from telethon import TelegramClient, events

API_ID = int(os.environ["38457834"])
API_HASH = os.environ["719a0b0b8b0f298ce02663ff3d99fde2"]
SESSION = os.environ.get("SESSION_NAME", "fragment_user")

client = TelegramClient(SESSION, API_ID, API_HASH)


@client.on(events.NewMessage(outgoing=True))
async def offer_handler(event):
    text = event.raw_text or ""

    if not text.startswith("!оффер "):
        return

    parts = text.split()

    if len(parts) != 4:
        await event.reply(
            "Формат: !оффер ССЫЛКА ЦЕНА ВАЛЮТА"
        )
        return

    _, link, price, currency = parts

    if currency.lower() not in ("stars", "gram", "usdt"):
        await event.reply("Валюта: Stars, GRAM или USDT.")
        return

    if not link.startswith(("https://t.me/", "https://fragment.com/")):
        await event.reply("Проверь ссылку на NFT.")
        return

    try:
        if float(price.replace(",", ".")) <= 0:
            raise ValueError
    except ValueError:
        await event.reply("Укажи корректную цену.")
        return

    await event.delete()

    offer = (
        "🎁 NFT OFFER\n\n"
        f"🔗 NFT: {link}\n"
        f"💰 Цена: {price} {currency}\n"
    )

    await client.send_message(event.chat_id, offer)


async def main():
    await client.start()
    print("Fragment automation started.")
    await client.run_until_disconnected()


asyncio.run(main())
