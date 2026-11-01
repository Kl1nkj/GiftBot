"""Telegram-аккаунт пользователя (Telethon). Нужен официальному маркету и для входа в MRKT."""
from urllib.parse import parse_qs, urlparse

from telethon import TelegramClient, functions, types, utils

from config import API_HASH, API_ID, SESSION_PATH, USER_CLIENT_ENABLED

_client: TelegramClient | None = None


async def get_client() -> TelegramClient | None:
    global _client
    if not USER_CLIENT_ENABLED:
        return None
    if _client is None:
        _client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
        await _client.connect()
    if not _client.is_connected():
        await _client.connect()
    if not await _client.is_user_authorized():
        raise RuntimeError("аккаунт не авторизован — запустите один раз: python login.py")
    return _client


async def get_init_data(bot_username: str) -> str:
    """Открывает мини-приложение бота от имени аккаунта и возвращает tgWebAppData (initData)."""
    client = await get_client()
    if client is None:
        raise RuntimeError("нет API_ID/API_HASH в .env")
    bot = await client.get_input_entity(bot_username)
    result = await client(functions.messages.RequestMainWebViewRequest(peer=bot, bot=bot, platform="android"))
    fragment = urlparse(result.url).fragment
    data = parse_qs(fragment).get("tgWebAppData", [None])[0]
    if not data:
        raise RuntimeError(f"не удалось получить initData для @{bot_username}")
    return data


async def get_app_init_data(bot_username: str, short_name: str) -> str:
    """То же для именованного мини-приложения (t.me/<бот>/<short_name>), например t.me/portals_market_bot/market."""
    client = await get_client()
    if client is None:
        raise RuntimeError("нет API_ID/API_HASH в .env")
    bot = await client.get_input_entity(bot_username)
    app = types.InputBotAppShortName(bot_id=utils.get_input_user(bot), short_name=short_name)
    result = await client(functions.messages.RequestAppWebViewRequest(peer=bot, app=app, platform="android"))
    data = parse_qs(urlparse(result.url).fragment).get("tgWebAppData", [None])[0]
    if not data:
        raise RuntimeError(f"не удалось получить initData для t.me/{bot_username}/{short_name}")
    return data


async def disconnect() -> None:
    if _client is not None:
        await _client.disconnect()
