import html
import re

from models import Listing
from sources.base import Source

BASE = "https://fragment.com"

_ITEM_RE = re.compile(
    r'<a href="/gift/(?P<slug>[a-z0-9]+)-(?P<num>\d+)[^"]*" class="tm-grid-item">(?P<body>.*?)</a>',
    re.S,
)
_NAME_RE = re.compile(r'<span class="item-name">(.*?)</span>', re.S)
_PRICE_RE = re.compile(r'tm-grid-item-value tm-value icon-before icon-ton">([\d,.]+)<')
_ATTR_RE = re.compile(r'attr%5B(Model|Backdrop|Symbol)%5D=[^"]*"[^>]*>([^<]+)</a>')


class FragmentSource(Source):
    name = "fragment"
    interval = 20

    async def fetch(self, wanted_collections):
        resp = await self.http.get(f"{BASE}/gifts", params={"sort": "listed", "filter": "sale"})
        resp.raise_for_status()
        items = []
        for m in _ITEM_RE.finditer(resp.text):
            body = m.group("body")
            name = _NAME_RE.search(body)
            price = _PRICE_RE.search(body)
            slug, num = m.group("slug"), int(m.group("num"))
            items.append(
                Listing(
                    market=self.name,
                    item_id=f"{slug}-{num}",
                    collection=html.unescape(name.group(1)).strip() if name else slug,
                    number=num,
                    price=float(price.group(1).replace(",", "")) if price else None,
                    url=f"{BASE}/gift/{slug}-{num}",
                    needs_details=True,
                )
            )
        return items

    async def load_details(self, item: Listing) -> None:
        resp = await self.http.get(item.url)
        resp.raise_for_status()
        attrs = {k: html.unescape(v).strip() for k, v in _ATTR_RE.findall(resp.text)}
        item.model = attrs.get("Model")
        item.backdrop = attrs.get("Backdrop")
        item.symbol = attrs.get("Symbol")
        item.needs_details = False
