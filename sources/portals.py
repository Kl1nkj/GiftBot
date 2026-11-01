from models import Listing, lot_url
from sources.base import Source

API = "https://portal-market.com/api/nfts/search"


class PortalsSource(Source):
    name = "portals"
    interval = 10

    async def fetch(self, wanted_collections):
        resp = await self.http.get(
            API,
            params={"offset": 0, "limit": 50, "sort_by": "listed_at desc", "status": "listed"},
            headers={"Origin": "https://portal-market.com", "Referer": "https://portal-market.com/"},
        )
        resp.raise_for_status()
        items = []
        for nft in resp.json().get("results", []):
            attrs = {a.get("type"): a.get("value") for a in nft.get("attributes") or []}
            try:
                price = float(nft["price"])
            except (KeyError, TypeError, ValueError):
                price = None
            items.append(
                Listing(
                    market=self.name,
                    item_id=str(nft.get("id")),
                    collection=nft.get("name") or "",
                    number=nft.get("external_collection_number"),
                    price=price,
                    model=attrs.get("model"),
                    backdrop=attrs.get("backdrop"),
                    symbol=attrs.get("symbol"),
                    url=lot_url(self.name, nft.get("id")),
                )
            )
        return items
