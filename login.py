"""Однократный вход в Telegram-аккаунт для официального маркета и MRKT.
Запустите: python login.py — введите номер телефона и код из Telegram. Появится файл user.session."""
import asyncio

from telethon import TelegramClient

from config import API_HASH, API_ID, SESSION_PATH


async def main():
    if not (API_ID and API_HASH):
        raise SystemExit("Заполните API_ID и API_HASH в .env (получить на https://my.telegram.org → API development tools)")
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    await client.start()
    me = await client.get_me()
    print(f"Готово, вошли как {me.first_name} (@{me.username}). Файл сессии: user.session")
    await client.disconnect()


asyncio.run(main())
