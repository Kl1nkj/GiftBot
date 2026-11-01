import re
from dataclasses import dataclass, field

MARKETS = {
    "telegram": "Telegram Market",
    "portals": "Portals",
    "tonnel": "Tonnel",
    "mrkt": "MRKT",
    "fragment": "Fragment",
    "getgems": "GetGems",
    "marketapp": "MarketApp",
}

# TON переименован в GRAM (июнь 2026), курс 1:1 — площадки пишут то одно, то другое
TON_CURRENCIES = {"TON", "GRAM"}


def is_ton(currency: str | None) -> bool:
    return (currency or "TON").upper() in TON_CURRENCIES

_PERCENT_RE = re.compile(r"\s*\([\d.,]+%\)\s*$")


def norm(value: str | None) -> str:
    """Приводит название к виду для сравнения: без процента редкости, регистра и лишних пробелов."""
    if not value:
        return ""
    value = _PERCENT_RE.sub("", value)
    return " ".join(value.lower().replace("’", "'").split())


def strip_rarity(value: str | None) -> str | None:
    return _PERCENT_RE.sub("", value).strip() if value else value


def lot_url(market: str, item_id: str) -> str | None:
    """Ссылка прямо на лот в маркете — тот же формат, что у их кнопки «Поделиться»."""
    if market == "portals":
        return f"https://t.me/portals_market_bot/market?startapp=gift_{item_id}"
    if market == "tonnel":
        return f"https://t.me/tonnel_network_bot/gift?startapp={item_id}"
    if market == "fragment":
        return f"https://fragment.com/gift/{item_id}"
    return None


def portals_collection_url(collection_id: str) -> str:
    return f"https://t.me/portals_market_bot/market?startapp=collection-{collection_id}"


def slugify(collection: str) -> str:
    return re.sub(r"[^a-z0-9]", "", collection.lower())


@dataclass
class Listing:
    market: str
    item_id: str
    collection: str
    number: int | None
    price: float | None  # в TON (или эквивалент)
    currency: str = "TON"
    model: str | None = None
    backdrop: str | None = None
    symbol: str | None = None
    url: str | None = None
    price_text: str | None = None  # как показывать цену, если она не в TON
    # Fragment отдаёт атрибуты только на странице подарка, их подгружаем лениво
    needs_details: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.market}:{self.item_id}:{self.price}"

    @property
    def nft_link(self) -> str | None:
        if self.number is None:
            return None
        return f"https://t.me/nft/{re.sub(r'[^A-Za-z0-9]', '', self.collection)}-{self.number}"

    @property
    def image(self) -> str | None:
        if self.number is None:
            return None
        return f"https://nft.fragment.com/gift/{slugify(self.collection)}-{self.number}.medium.jpg"
