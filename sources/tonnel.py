import json

from models import Listing, is_ton, lot_url, strip_rarity
from sources.base import Source

API = "https://gifts2.tonnel.network/api/pageGifts"


class TonnelSource(Source):
    name = "tonnel"
    interval = 10

    async def fetch(self, wanted_collections):
        body = {
            "page": 1,
            "limit": 30,
            "sort": json.dumps({"message_post_time": -1, "gift_id": -1}),
            "filter": json.dumps({"price": {"$exists": True}, "buyer": {"$exists": False}, "asset": "TON"}),
            "ref": 0,
            "price_range": None,
            "user_auth": "",
        }
        resp = await self.http.post(
            API,
            json=body,
            headers={"Origin": "https://market.tonnel.network", "Referer": "https://market.tonnel.network/"},
        )
        resp.raise_for_status()
        items = []
        for g in resp.json():
            if g.get("status") not in (None, "forsale"):
                continue
            asset = g.get("asset") or "TON"
            price = g.get("price")
            items.append(
                Listing(
                    market=self.name,
                    item_id=str(g.get("gift_id")),
                    collection=g.get("name") or "",
                    number=g.get("gift_num"),
                    # фильтры по цене считаются в TON, поэтому лоты в других валютах сравниваем только по атрибутам
                    price=float(price) if price is not None and is_ton(asset) else None,
                    price_text=None if is_ton(asset) else f"{price} {asset}",
                    currency=asset,
                    model=strip_rarity(g.get("model")),
                    backdrop=strip_rarity(g.get("backdrop")),
                    symbol=strip_rarity(g.get("symbol")),
                    url=lot_url(self.name, g.get("gift_id")),
                )
            )
        return items
