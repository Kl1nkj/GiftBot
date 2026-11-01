import re

from config import GETGEMS_API_KEY
from models import Listing, is_ton
from sources.base import Source

API = "https://api.getgems.io/public-api/v1/nfts/offchain/on-sale/gifts"
_NAME_RE = re.compile(r"^(?P<collection>.+?)\s*#(?P<num>\d+)$")


class GetGemsSource(Source):
    name = "getgems"
    interval = 30

    @property
    def available(self) -> bool:
        return bool(GETGEMS_API_KEY)

    @property
    def unavailable_reason(self) -> str:
        return "нет GETGEMS_API_KEY в .env"

    async def fetch(self, wanted_collections):
        items = []
        cursor = None
        for _ in range(3):  # до 300 лотов за опрос
            params = {"limit": 100}
            if cursor:
                params["after"] = cursor
            resp = await self.http.get(API, params=params, headers={"Authorization": GETGEMS_API_KEY})
            resp.raise_for_status()
            data = resp.json().get("response", {})
            for nft in data.get("items", []):
                sale = nft.get("sale") or {}
                if sale.get("type") != "FixPriceSale":
                    continue
                m = _NAME_RE.match(nft.get("name") or "")
                if not m:
                    continue
                currency = sale.get("currency") or "TON"
                price_nano = int(sale.get("fullPrice") or 0)
                attrs = {a.get("traitType"): a.get("value") for a in nft.get("attributes") or []}
                items.append(
                    Listing(
                        market=self.name,
                        item_id=nft["address"],
                        collection=m.group("collection"),
                        number=int(m.group("num")),
                        price=price_nano / 1e9 if is_ton(currency) else None,
                        price_text=None if is_ton(currency) else currency,
                        currency=currency,
                        model=attrs.get("Model"),
                        backdrop=attrs.get("Backdrop"),
                        symbol=attrs.get("Symbol"),
                        url=f"https://getgems.io/nft/{nft['address']}",
                    )
                )
            cursor = data.get("cursor")
            if not cursor:
                break
        return items
