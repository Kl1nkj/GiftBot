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


def number_pattern(n: int | None, supply: int | None = None) -> tuple[int, str | None]:
    """Насколько «красивый» номер подарка: (уровень 0–3, описание). Коллекционеры за такие часто доплачивают.
    3 — топ (#1–#100, одинаковые цифры 7777), 2 — заметный (повтор 183183, круглый 50000, 1234, до #1000),
    1 — слабый (зеркальный 12321, оканчивается на 000).
    supply — тираж коллекции: «до #1000» ценится только в больших коллекциях (в тираже 3000 это треть)."""
    if n is None or n <= 0:
        return 0, None
    s = str(n)
    if n <= 9:
        return 3, f"легендарный номер #{n} — один из первых"
    if len(s) >= 2 and len(set(s)) == 1:  # 11, 555, 6666, 77777
        words = {2: "две", 3: "три", 4: "четыре", 5: "пять", 6: "шесть", 7: "семь"}
        return 3, f"{words.get(len(s), str(len(s)))} одинаковые цифры #{n}"
    if n <= 100:
        return 3, f"очень низкий номер #{n}"
    for size in range(1, len(s) // 2 + 1):  # 183183, 1212, 121212
        if len(s) % size == 0 and s == s[:size] * (len(s) // size):
            return 2, f"повторяющийся номер #{n} ({'-'.join([s[:size]] * (len(s) // size))})"
    if len(s) >= 4 and s[1:] == "0" * (len(s) - 1):
        return 2, f"круглый номер #{n}"
    digits = [int(c) for c in s]
    steps = {b - a for a, b in zip(digits, digits[1:])}
    if len(s) >= 4 and steps in ({1}, {-1}):
        return 2, f"номер-последовательность #{n}"
    if n <= 1000:
        if supply is None or supply >= 20000:
            return 2, f"низкий номер #{n}"
        return 1, f"низкий номер #{n} (в тираже {supply})"
    if len(s) >= 4 and s == s[::-1]:
        return 1, f"зеркальный номер #{n}"
    if len(s) >= 5 and s.endswith("000"):
        return 1, f"номер на 000 — #{n}"
    return 0, None


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
