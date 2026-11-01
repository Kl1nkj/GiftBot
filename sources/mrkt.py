from config import USER_CLIENT_ENABLED
from models import Listing
from sources.base import Source
from user_client import get_init_data

API = "https://api.tgmrkt.io/api/v1"
BOT = "mrkt"


class MrktSource(Source):
    name = "mrkt"
    interval = 15

    def __init__(self):
        super().__init__()
        self.token: str | None = None

    @property
    def available(self) -> bool:
        return USER_CLIENT_ENABLED

    @property
    def unavailable_reason(self) -> str:
        return "нет API_ID/API_HASH в .env (MRKT пускает только с авторизацией Telegram)"

    async def _login(self) -> None:
        init_data = await get_init_data(BOT)
        resp = await self.http.post(f"{API}/auth", json={"data": init_data})
        resp.raise_for_status()
        self.token = resp.json()["token"]

    async def _request(self, body: dict) -> dict:
        if not self.token:
            await self._login()
        resp = await self.http.post(f"{API}/gifts/saling", json=body, headers={"Authorization": self.token})
        if resp.status_code in (401, 403):
            self.token = None
            await self._login()
            resp = await self.http.post(f"{API}/gifts/saling", json=body, headers={"Authorization": self.token})
        resp.raise_for_status()
        return resp.json()

    async def fetch(self, wanted_collections):
        data = await self._request(
            {
                "count": 20,
                "cursor": "",
                "collectionNames": [],
                "modelNames": [],
                "backdropNames": [],
                "symbolNames": [],
                "minPrice": None,
                "maxPrice": None,
                "mintable": None,
                "number": None,
                "ordering": "None",  # порядок по умолчанию в MRKT — сначала новые
                "lowToHigh": False,
                "query": None,
            }
        )
        items = []
        for g in data.get("gifts", []):
            price = g.get("salePrice")
            items.append(
                Listing(
                    market=self.name,
                    item_id=str(g.get("id")),
                    collection=g.get("collectionTitle") or g.get("title") or g.get("collectionName") or "",
                    number=g.get("number"),
                    price=price / 1e9 if price else None,
                    model=g.get("modelTitle") or g.get("modelName"),
                    backdrop=g.get("backdropName"),
                    symbol=g.get("symbolName"),
                    url="https://t.me/mrkt",
                )
            )
        return items
