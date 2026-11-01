import re

from config import MARKETAPP_API_TOKEN
from models import Listing, is_ton
from sources.base import Source

API = "https://api.marketapp.ws/v1/gifts/onsale/"
_NAME_RE = re.compile(r"^(?P<collection>.+?)\s*#(?P<num>\d+)$")


class MarketAppSource(Source):
    """Подарки, выставленные в блокчейне (MarketApp и другие ончейн-площадки), через MarketApp API."""
    name = "marketapp"
    interval = 30

    @property
    def available(self) -> bool:
        return bool(MARKETAPP_API_TOKEN)

    @property
    def unavailable_reason(self) -> str:
        return "нет MARKETAPP_API_TOKEN в .env"

    async def fetch(self, wanted_collections):
        resp = await self.http.get(
            API, params={"sort_by": "recently_touch", "limit": 100}, headers={"Authorization": MARKETAPP_API_TOKEN}
        )
        resp.raise_for_status()
        items = []
        for nft in resp.json().get("items", []):
            m = _NAME_RE.match(nft.get("name") or "")
            if not m or not nft.get("min_bid"):
                continue
            attrs = {str(a.get("trait_type", "")).lower(): a.get("value") for a in nft.get("attributes") or []}
            currency = nft.get("currency") or "GRAM"
            price = float(nft["min_bid"])
            items.append(
                Listing(
                    market=self.name,
                    item_id=nft["address"],
                    collection=m.group("collection"),
                    number=nft.get("item_num") or int(m.group("num")),
                    price=price if is_ton(currency) else None,
                    price_text=None if is_ton(currency) else f"{price:g} {currency}",
                    currency=currency,
                    model=attrs.get("model"),
                    backdrop=attrs.get("backdrop"),
                    symbol=attrs.get("symbol"),
                )
            )
        return items
