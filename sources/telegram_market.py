import asyncio
import time

from telethon import functions, types

import config
from config import USER_CLIENT_ENABLED


def set_stars_per_ton(value: float) -> None:
    """Обновляется монитором по живому курсу TON (если STARS_PER_TON не задан в .env)."""
    config.STARS_PER_TON = value
from models import Listing, norm
from sources.base import Source
from user_client import get_client

CATALOG_TTL = 3600
WILDCARD_BATCH = 8  # сколько коллекций опрашивать за цикл, если в фильтрах есть «любая коллекция»


class TelegramMarketSource(Source):
    name = "telegram"
    interval = 30

    def __init__(self):
        super().__init__()
        self.catalog: dict[str, tuple[int, str]] = {}  # norm(title) -> (gift_id, title)
        self.catalog_ts = 0.0
        self.rr_index = 0
        self.seen_collections: set[int] = set()

    @property
    def available(self) -> bool:
        return USER_CLIENT_ENABLED

    @property
    def unavailable_reason(self) -> str:
        return "нет API_ID/API_HASH в .env"

    async def _refresh_catalog(self, client) -> None:
        if self.catalog and time.time() - self.catalog_ts < CATALOG_TTL:
            return
        result = await client(functions.payments.GetStarGiftsRequest(hash=0))
        catalog = {}
        for gift in getattr(result, "gifts", []):
            if isinstance(gift, types.StarGift) and gift.title and gift.availability_resale:
                catalog[norm(gift.title)] = (gift.id, gift.title)
        self.catalog = catalog
        self.catalog_ts = time.time()

    async def fetch(self, wanted_collections):
        client = await get_client()
        await self._refresh_catalog(client)

        if wanted_collections is None:
            keys = sorted(self.catalog)
            if not keys:
                return []
            start = self.rr_index % len(keys)
            batch = (keys + keys)[start:start + min(WILDCARD_BATCH, len(keys))]
            self.rr_index = start + len(batch)
        else:
            batch = [k for k in wanted_collections if k in self.catalog]

        items = []
        for key in batch:
            gift_id, title = self.catalog[key]
            result = await client(functions.payments.GetResaleStarGiftsRequest(gift_id=gift_id, offset="", limit=30))
            first_visit = gift_id not in self.seen_collections
            self.seen_collections.add(gift_id)
            for gift in result.gifts:
                if isinstance(gift, types.StarGiftUnique):
                    item = self._to_listing(gift, title)
                    # коллекцию опрашиваем впервые — уже выставленные лоты запоминаем без уведомлений
                    item.extra["seed"] = first_visit
                    items.append(item)
            await asyncio.sleep(1)  # бережём аккаунт от FloodWait
        return items

    def _to_listing(self, gift: types.StarGiftUnique, title: str) -> Listing:
        attrs = {}
        for a in gift.attributes:
            if isinstance(a, types.StarGiftAttributeModel):
                attrs["model"] = a.name
            elif isinstance(a, types.StarGiftAttributeBackdrop):
                attrs["backdrop"] = a.name
            elif isinstance(a, types.StarGiftAttributePattern):
                attrs["symbol"] = a.name

        stars = ton = None
        for amount in gift.resell_amount or []:
            if isinstance(amount, types.StarsTonAmount):
                ton = amount.amount / 1e9
            elif isinstance(amount, types.StarsAmount):
                stars = amount.amount

        if ton is not None:
            price, price_text = ton, None
            if stars is not None and not gift.resale_ton_only:
                price_text = f"{ton:g} TON / {stars} ⭐"
        elif stars is not None:
            rate = config.STARS_PER_TON
            price, price_text = stars / rate, f"{stars} ⭐ (~{stars / rate:.2f} TON)"
        else:
            price, price_text = None, None

        return Listing(
            market=self.name,
            item_id=str(gift.id),
            collection=gift.title or title,
            number=gift.num,
            price=price,
            price_text=price_text,
            model=attrs.get("model"),
            backdrop=attrs.get("backdrop"),
            symbol=attrs.get("symbol"),
            url=f"https://t.me/nft/{gift.slug}",
        )
