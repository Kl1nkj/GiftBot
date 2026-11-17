"""Веб-сервер мини-приложения: отдаёт страницу, каталог подарков (из Portals) и API фильтров."""
import asyncio
import hashlib
import hmac
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import parse_qsl, quote

from aiohttp import web
from curl_cffi.requests import AsyncSession

from config import BOT_TOKEN, OWNER_IDS, WEBAPP_DEV_NO_AUTH
from models import MARKETS
from monitor import listing_from_hit
from storage import GiftFilter, Storage

log = logging.getLogger("webapp")

PORTALS = "https://portal-market.com/api"
ASSETS = "https://cdn.changes.tg/gifts"
STATIC_DIR = Path(__file__).resolve().parent / "webapp"
CACHE_TTL = 600
INIT_DATA_TTL = 86400


def check_init_data(init_data: str) -> dict | None:
    """Проверка подписи Telegram.WebApp.initData (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)."""
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return None
    try:
        if time.time() - int(pairs.get("auth_date", 0)) > INIT_DATA_TTL:
            return None
        user = json.loads(pairs.get("user", "{}"))
    except (ValueError, TypeError):
        return None
    return user if isinstance(user, dict) else None


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path.startswith("/api/") and not WEBAPP_DEV_NO_AUTH:
        user = check_init_data(request.headers.get("X-Init-Data", ""))
        if not user or user.get("id") not in OWNER_IDS:
            log.warning("Мини-приложение: нет доступа для id=%s (владельцы: %s)", user and user.get("id"), sorted(OWNER_IDS))
            return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


class Catalog:
    """Каталог коллекций и атрибутов с кэшем, чтобы не дёргать Portals на каждое открытие."""

    def __init__(self):
        self.http = AsyncSession(impersonate="chrome", timeout=25)
        self._cache: dict[str, tuple[float, object]] = {}

    async def _get_json(self, url: str, params: dict | None = None):
        key = url + json.dumps(params or {}, sort_keys=True)
        cached = self._cache.get(key)
        if cached and time.time() - cached[0] < CACHE_TTL:
            return cached[1]
        resp = await self.http.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        self._cache[key] = (time.time(), data)
        return data

    async def collections(self) -> list[dict]:
        data = await self._get_json(f"{PORTALS}/collections", {"limit": 500})
        result = []
        for c in data.get("collections", []):
            if c.get("is_premarket"):
                continue
            result.append(
                {
                    "name": c["name"],
                    "short": c["short_name"],
                    "floor": _num(c.get("floor_price")),
                    "listed": c.get("listed_count") or 0,
                    "supply": c.get("supply") or 0,
                    "is_new": bool(c.get("is_new")),
                    "image": f"https://fragment.com/file/gifts/{c['short_name']}/thumb.webp",
                }
            )
        return result

    async def backdrops(self) -> list[dict]:
        colors = {b["name"]: b.get("hex", {}) for b in await self._get_json(f"{ASSETS}/backdrops.json")}
        stats = {b["name"]: b for b in await self._get_json(f"{PORTALS}/collections/filters/backdrops")}
        names = sorted(set(colors) | set(stats))
        return [
            {
                "name": n,
                "rarity": stats.get(n, {}).get("rarityPermille") or 0,
                "floor": _num(stats.get(n, {}).get("floor_price")),
                "center": colors.get(n, {}).get("centerColor"),
                "edge": colors.get(n, {}).get("edgeColor"),
            }
            for n in names
        ]

    async def attributes(self, shorts: list[str]) -> dict:
        names = {c["short"]: c["name"] for c in await self.collections()}
        shorts = [s for s in shorts if s in names][:10]
        if not shorts:
            return {"models": [], "symbols": []}
        data = await self._get_json(f"{PORTALS}/collections/filters", {"short_names": ",".join(shorts)})
        models, symbols = [], []
        for short, attrs in data.get("collections", {}).items():
            coll = names.get(short, short)
            asset_name = _asset(coll)
            for m in attrs.get("models", []):
                models.append(
                    {
                        "name": m["name"],
                        "collection": coll,
                        "rarity": m.get("rarity_per_mille") or 0,
                        "floor": _num(m.get("floor_price")),
                        "image": f"{ASSETS}/models/{asset_name}/png/{_asset(m['name'])}.png",
                    }
                )
            for s in attrs.get("symbols", []):
                symbols.append(
                    {
                        "name": s["name"],
                        "collection": coll,
                        "rarity": s.get("rarity_per_mille") or 0,
                        "floor": _num(s.get("floor_price")),
                        "image": f"{ASSETS}/patterns/{asset_name}/png/{_asset(s['name'])}.png",
                    }
                )
        models.sort(key=lambda x: (x["collection"], x["rarity"], x["name"]))
        symbols.sort(key=lambda x: (x["collection"], x["rarity"], x["name"]))
        return {"models": models, "symbols": symbols}

    async def all_symbols(self) -> list[dict]:
        data = await self._get_json(f"{PORTALS}/collections/filters/symbols")
        seen = {}
        for s in data if isinstance(data, list) else []:
            seen.setdefault(s["name"], {"name": s["name"], "rarity": s.get("rarityPermille") or 0, "floor": _num(s.get("floor_price"))})
        return sorted(seen.values(), key=lambda x: x["name"])


def _asset(name: str) -> str:
    # Portals пишет «Durov’s Cap», а в CDN картинок — «Durov's Cap»
    return quote(name.replace("’", "'"))


def _num(value) -> float | None:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _filter_from_json(data: dict, filter_id: int | None = None) -> GiftFilter:
    def strings(key):
        return [str(v).strip() for v in data.get(key) or [] if str(v).strip()]

    def number(key, cast=float):
        value = data.get(key)
        return cast(value) if value not in (None, "") else None

    markets = [m for m in strings("markets") if m in MARKETS]
    return GiftFilter(
        id=filter_id,
        collections=strings("collections"),
        models=strings("models"),
        backdrops=strings("backdrops"),
        symbols=strings("symbols"),
        min_price=number("min_price"),
        max_price=number("max_price"),
        max_number=number("max_number", int),
        markets=markets,
        enabled=bool(data.get("enabled", True)),
    )


def create_app(storage: Storage, monitor) -> web.Application:
    catalog = Catalog()
    routes = web.RouteTableDef()

    @routes.get("/")
    async def index(request):
        return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @routes.get("/api/meta")
    async def meta(request):
        return web.json_response({"markets": MARKETS, "paused": monitor.paused})

    @routes.get("/api/collections")
    async def collections(request):
        return web.json_response(await catalog.collections())

    @routes.get("/api/backdrops")
    async def backdrops(request):
        return web.json_response(await catalog.backdrops())

    @routes.get("/api/attributes")
    async def attributes(request):
        shorts = [s for s in request.query.get("collections", "").split(",") if s]
        if shorts:
            return web.json_response(await catalog.attributes(shorts))
        return web.json_response({"models": [], "symbols": await catalog.all_symbols()})

    @routes.get("/api/filters")
    async def list_filters(request):
        return web.json_response([asdict(f) for f in storage.list_filters()])

    @routes.post("/api/filters")
    async def create_filter(request):
        f = _filter_from_json(await request.json())
        f.id = storage.add_filter(f)
        return web.json_response(asdict(f))

    @routes.put("/api/filters/{id}")
    async def update_filter(request):
        f = _filter_from_json(await request.json(), int(request.match_info["id"]))
        storage.update_filter(f)
        return web.json_response(asdict(f))

    @routes.delete("/api/filters/{id}")
    async def delete_filter(request):
        storage.delete_filter(int(request.match_info["id"]))
        return web.json_response({"ok": True})

    @routes.get("/api/feed")
    async def feed(request):
        return web.json_response(storage.list_hits(int(request.query.get("limit", 100))))

    @routes.get("/api/deals")
    async def deals(request):
        since = int(time.time()) - int(float(request.query.get("hours", 24)) * 3600)
        hits = [h for h in storage.list_hits(1000, since) if (h.get("analysis") or {}).get("ok")]
        hits.sort(key=lambda h: h["analysis"]["roi"], reverse=True)
        return web.json_response(hits[:100])

    @routes.get("/api/hunt")
    async def get_hunt(request):
        return web.json_response(monitor.analyzer.deal_settings())

    @routes.post("/api/hunt")
    async def set_hunt(request):
        try:
            return web.json_response(monitor.analyzer.save_deal_settings(await request.json()))
        except (TypeError, ValueError):
            return web.json_response({"error": "bad number"}, status=400)

    @routes.post("/api/reanalyze/{id}")
    async def reanalyze(request):
        hit = storage.get_hit(int(request.match_info["id"]))
        if not hit:
            return web.json_response({"error": "not found"}, status=404)
        analysis = await asyncio.wait_for(monitor.analyzer.analyze(listing_from_hit(hit), fresh=True, check_listed=True), 30)
        hit["analysis"] = analysis.to_dict()
        storage.update_hit(hit["id"], hit)
        return web.json_response(hit)

    @routes.get("/api/status")
    async def status(request):
        return web.json_response(
            {
                "paused": monitor.paused,
                "sent": monitor.sent_total,
                "deals": monitor.deals_total,
                "sources": [
                    {
                        "key": s.name,
                        "title": MARKETS[s.name],
                        "available": s.available,
                        "reason": s.unavailable_reason if not s.available else None,
                        "error": s.last_error,
                        "last_ok": s.last_ok,
                        "count": s.last_count,
                    }
                    for s in monitor.sources
                ],
            }
        )

    @routes.post("/api/pause")
    async def pause(request):
        data = await request.json()
        storage.set_setting("paused", "1" if data.get("paused") else "0")
        return web.json_response({"paused": monitor.paused})

    app = web.Application(middlewares=[auth_middleware])
    app.add_routes(routes)

    async def on_cleanup(app):
        await catalog.http.close()

    app.on_cleanup.append(on_cleanup)
    return app


async def start_webapp(storage: Storage, monitor, host: str, port: int) -> web.AppRunner:
    if WEBAPP_DEV_NO_AUTH:  # без проверки входа API доступен всем, кто достучится, — только с этого компьютера
        host = "127.0.0.1"
    runner = web.AppRunner(create_app(storage, monitor))
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    log.info("Мини-приложение слушает http://%s:%s", host, port)
    return runner
