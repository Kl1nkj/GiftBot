import os
import re
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _int(name: str, default: int = 0) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
# Владельцы бота: один или несколько Telegram ID через запятую
OWNER_IDS = {int(x) for x in re.split(r"[,\s]+", os.getenv("OWNER_ID", "")) if x.strip().isdigit()}

# Telegram-аккаунт (нужен для официального маркета и MRKT). Необязательно.
API_ID = _int("API_ID")
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_PATH = str(BASE_DIR / "user")

# Ключ GetGems Public API (https://getgems.io → API). Необязательно.
GETGEMS_API_KEY = os.getenv("GETGEMS_API_KEY", "").strip()
# Токен MarketApp API (https://marketapp.org/api-token): лоты Fragment/GetGems/MarketApp в блокчейне и их продажи
MARKETAPP_API_TOKEN = os.getenv("MARKETAPP_API_TOKEN", "").strip()

# Сколько звёзд считать за 1 TON для лотов официального маркета с ценой в звёздах.
# Если не задано — считается по живому курсу TON/USD и цене звезды STAR_USD (обновляется раз в час).
STARS_PER_TON_FIXED = bool(os.getenv("STARS_PER_TON", "").strip())
STARS_PER_TON = float(os.getenv("STARS_PER_TON", "") or "200")
STAR_USD = float(os.getenv("STAR_USD", "0.015"))

# Мини-приложение: внешний https-адрес (например https://mygifts.duckdns.org) и локальный порт веб-сервера
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip().rstrip("/")
WEBAPP_HOST = os.getenv("WEBAPP_HOST", "127.0.0.1").strip()
WEBAPP_PORT = _int("WEBAPP_PORT", 8080)
# Без сервера: если WEBAPP_URL пуст, бот сам поднимает бесплатный туннель Cloudflare (нужен cloudflared)
WEBAPP_TUNNEL = os.getenv("WEBAPP_TUNNEL", "1") == "1"
# Постоянный адрес без сервера: бесплатный аккаунт ngrok.com → Your Authtoken и Domains (один домен бесплатно)
NGROK_AUTHTOKEN = os.getenv("NGROK_AUTHTOKEN", "").strip()
NGROK_DOMAIN = os.getenv("NGROK_DOMAIN", "").strip()
# Только для проверки на своём компьютере в обычном браузере: отключает проверку входа через Telegram
WEBAPP_DEV_NO_AUTH = os.getenv("WEBAPP_DEV_NO_AUTH", "") == "1"

# Анализ перепродажи: комиссия продавца на площадках (в %), формат "portals:5,tonnel:6"
_DEFAULT_FEES = "portals:5,tonnel:6,fragment:5,mrkt:5,getgems:5,marketapp:5,telegram:20"
SELL_FEES = {
    k.strip(): float(v) / 100
    for k, v in (pair.split(":") for pair in os.getenv("SELL_FEES", _DEFAULT_FEES).split(",") if ":" in pair)
}
# Комиссия покупателя на площадках (в %), формат как у SELL_FEES. По умолчанию 0 — поставьте свои,
# если площадка берёт за покупку или вывод (например "tonnel:1")
BUY_FEES = {
    k.strip(): float(v) / 100
    for k, v in (pair.split(":") for pair in os.getenv("BUY_FEES", "").split(",") if ":" in pair)
}
# Сколько стоит перевести подарок с одной площадки на другую (вывод + перевод в Telegram), в TON
TRANSFER_COST_TON = float(os.getenv("TRANSFER_COST_TON", "0.15"))

DB_PATH = str(BASE_DIR / "data.db")

USER_CLIENT_ENABLED = bool(API_ID and API_HASH)
