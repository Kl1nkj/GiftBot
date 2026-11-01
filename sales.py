"""Реальные продажи — за сколько модель действительно покупают, а не за сколько её выставляют.

Источники:
  • Portals и Tonnel — история сделок по модели. Площадки отдают её только вошедшему пользователю,
    поэтому бот открывает их мини-приложения от имени вашего аккаунта (API_ID/API_HASH + python login.py);
  • MarketApp — продажи в блокчейне (Fragment, GetGems, MarketApp) с моделью и фоном, нужен MARKETAPP_API_TOKEN;
  • Telegram — по конкретному подарку: последняя продажа и средняя цена (официальная оценка Telegram),
    тоже через ваш аккаунт.
Всё необязательно: чего нет — того просто не будет в анализе."""
import asyncio
import logging
import re
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import datetime

from curl_cffi.requests import AsyncSession

from config import MARKETAPP_API_TOKEN, USER_CLIENT_ENABLED
from models import is_ton, lot_url, norm, strip_rarity

log = logging.getLogger("sales")

PORTALS_ACTIONS = "https://portal-market.com/api/market/actions/"
PORTALS_HEADERS = {"Origin": "https://portal-market.com", "Referer": "https://portal-market.com/"}
TONNEL_HISTORY = "https://gifts2.tonnel.network/api/saleHistory"
TONNEL_HEADERS = {"Origin": "https://market.tonnel.network", "Referer": "https://market.tonnel.network/"}
MARKETAPP = "https://api.marketapp.ws/v1"
TON_RATES = "https://tonapi.io/v2/rates"

# мини-приложения, которые бот открывает от имени аккаунта, чтобы войти в площадку
PORTALS_APP = ("portals_market_bot", "market")
TONNEL_APP = ("tonnel_network_bot", "gift")

SALES_DAYS = 30
SALES_TTL = 300
INIT_DATA_TTL = 20 * 60  # площадки не принимают слишком старый вход
MARKETAPP_PAGES = 3  # до 300 последних продаж коллекции
VALUE_TTL = 600


@dataclass
class Sale:
    market: str
    ts: int
    price: float
    number: int | None = None
    backdrop: str | None = None
    symbol: str | None = None
    url: str | None = None


@dataclass
class SalesSummary:
    sales: list[Sale]
    failed: list[str]  # площадки, которые не ответили
    missing: list[str]  # площадки, для которых нет входа/токена

    @property
    def prices(self) -> list[float]:
        return [s.price for s in self.sales]

    @property
    def median(self) -> float | None:
        return statistics.median(self.prices) if self.sales else None


def _ts(value) -> int | None:
    if isinstance(value, (int, float)):
        return int(value / 1000 if value > 1e11 else value)
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _attrs(items, key="trait_type", value="value") -> dict[str, str]:
    return {str(a.get(key, "")).lower(): a.get(value) for a in items or []}


class SalesHistory:
    def __init__(self, http: AsyncSession, portals_get=None):
        self.http = http
        self._portals_get = portals_get  # общий для всех запросов к Portals ограничитель частоты
        self._init: dict[tuple[str, str], tuple[float, str]] = {}
        self._cache: dict[tuple, tuple[float, SalesSummary]] = {}
        self._marketapp_colls: tuple[float, dict[str, str]] | None = None
        self._marketapp_sales: dict[str, tuple[float, list[tuple[str, Sale]]]] = {}
        self._rates: dict[str, tuple[float, float]] = {}
        self._values: dict[str, tuple[float, dict | None]] = {}

    @property
    def sources(self) -> dict[str, bool]:
        """Какие источники продаж подключены."""
        return {"portals": USER_CLIENT_ENABLED, "tonnel": USER_CLIENT_ENABLED,
                "marketapp": bool(MARKETAPP_API_TOKEN), "telegram": USER_CLIENT_ENABLED}

    async def _init_data(self, app: tuple[str, str], refresh: bool = False) -> str:
        from user_client import get_app_init_data  # telethon грузим, только если аккаунт подключён

        hit = self._init.get(app)
        if hit and not refresh and time.time() - hit[0] < INIT_DATA_TTL:
            return hit[1]
        data = await get_app_init_data(*app)
        self._init[app] = (time.time(), data)
        return data

    # ---------- Portals ----------
    async def _portals(self, collection: str, model: str) -> list[Sale]:
        params = {
            "offset": 0, "limit": 50, "action_types": "buy",
            "filter_by_collections": collection, "filter_by_models": model,
        }
        data = None
        for attempt in range(2):
            headers = {**PORTALS_HEADERS, "Authorization": f"tma {await self._init_data(PORTALS_APP, refresh=attempt > 0)}"}
            if self._portals_get:
                try:
                    data = await self._portals_get("/market/actions/", params, headers=headers)
                    break
                except Exception as e:
                    if "401" not in str(e) or attempt:
                        raise
            else:
                resp = await self.http.get(PORTALS_ACTIONS, params=params, headers=headers)
                if resp.status_code == 401 and not attempt:
                    continue
                resp.raise_for_status()
                data = resp.json()
                break
        sales = []
        for a in (data or {}).get("actions", []):
            nft = a.get("nft") or {}
            attrs = _attrs(nft.get("attributes"), "type", "value")
            if a.get("type") not in (None, "buy") or norm(attrs.get("model")) != norm(model):
                continue
            ts, price = _ts(a.get("created_at")), float(a.get("amount") or 0)
            if ts and price:
                sales.append(Sale("portals", ts, price, nft.get("external_collection_number"),
                                  attrs.get("backdrop"), attrs.get("symbol"), lot_url("portals", nft.get("id"))))
        return sales

    # ---------- Tonnel ----------
    async def _tonnel(self, collection: str, model: str) -> list[Sale]:
        sales = []
        for attempt in range(2):
            body = {
                "authData": await self._init_data(TONNEL_APP, refresh=attempt > 0),
                "page": 1, "limit": 50, "type": "ALL",
                "filter": {"gift_name": collection, "model": {"$regex": "^" + re.escape(model) + r" \("}},
                "sort": {"timestamp": -1, "gift_id": -1},
            }
            resp = await self.http.post(TONNEL_HISTORY, json=body, headers=TONNEL_HEADERS)
            if resp.status_code in (401, 403) and not attempt:
                continue
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and not attempt and "auth" in str(data.get("message", "")).lower():
                continue
            break
        for g in data if isinstance(data, list) else []:
            if norm(strip_rarity(g.get("model"))) != norm(model) or not is_ton(g.get("asset")):
                continue
            ts, price = _ts(g.get("timestamp")), float(g.get("price") or 0)
            if ts and price:
                sales.append(Sale("tonnel", ts, price, g.get("gift_num"), strip_rarity(g.get("backdrop")),
                                  strip_rarity(g.get("symbol"))))
        return sales

    # ---------- MarketApp (блокчейн: Fragment, GetGems, MarketApp) ----------
    async def _marketapp_get(self, path: str, params: dict | None = None):
        resp = await self.http.get(f"{MARKETAPP}{path}", params=params, headers={"Authorization": MARKETAPP_API_TOKEN})
        resp.raise_for_status()
        return resp.json()

    async def marketapp_collection(self, collection: str) -> str | None:
        """Адрес коллекции в блокчейне по названию."""
        if not self._marketapp_colls or time.time() - self._marketapp_colls[0] > 3600:
            data = await self._marketapp_get("/collections/gifts/")
            self._marketapp_colls = (time.time(), {norm(c.get("name")): c.get("address") for c in data or []})
        return self._marketapp_colls[1].get(norm(collection))

    async def _marketapp(self, collection: str, model: str) -> list[Sale]:
        address = await self.marketapp_collection(collection)
        if not address:
            return []
        cached = self._marketapp_sales.get(address)
        if not cached or time.time() - cached[0] > SALES_TTL:
            rows, cursor = [], None
            for _ in range(MARKETAPP_PAGES):
                params = {"collection_address": address, "limit": 100}
                if cursor:
                    params["cursor"] = cursor
                data = await self._marketapp_get("/gifts/history/", params)
                for ev in data.get("items", []):
                    d = ev.get("type_details") or {}
                    attrs = _attrs(ev.get("attributes"))
                    if ev.get("type") != "sold" or not is_ton(d.get("currency")) or not d.get("price"):
                        continue
                    num = re.search(r"#(\d+)", ev.get("name") or "")
                    market = (d.get("market_name") or "marketapp").lower()
                    rows.append((norm(attrs.get("model")), Sale(
                        market, int(ev.get("ts") or 0), float(d["price"]), int(num.group(1)) if num else None,
                        attrs.get("backdrop"), attrs.get("symbol"),
                    )))
                cursor = data.get("cursor")
                oldest = min((s.ts for _, s in rows), default=0)
                if not cursor or oldest < time.time() - SALES_DAYS * 86400:
                    break
            cached = (time.time(), rows)
            self._marketapp_sales[address] = cached
        return [s for m, s in cached[1] if m == norm(model)]

    # ---------- всё вместе ----------
    async def model_sales(self, collection: str, model: str, fresh: bool = False) -> SalesSummary:
        key = (norm(collection), norm(model))
        hit = self._cache.get(key)
        if hit and not fresh and time.time() - hit[0] < SALES_TTL:
            return hit[1]
        jobs, names, missing = [], [], []
        for name, func, enabled in (
            ("portals", self._portals, USER_CLIENT_ENABLED),
            ("tonnel", self._tonnel, USER_CLIENT_ENABLED),
            ("marketapp", self._marketapp, bool(MARKETAPP_API_TOKEN)),
        ):
            if enabled:
                jobs.append(func(collection, model))
                names.append(name)
            else:
                missing.append(name)
        results = await asyncio.gather(*jobs, return_exceptions=True)
        since = time.time() - SALES_DAYS * 86400
        sales, failed = [], []
        for name, r in zip(names, results):
            if isinstance(r, Exception) and "не авторизован" in str(r):
                missing.append(name)  # API_ID есть, но в аккаунт ещё не вошли (python login.py)
            elif isinstance(r, Exception):
                log.warning("продажи %s / %s на %s: %s", collection, model, name, r)
                failed.append(name)
            else:
                sales.extend(s for s in r if s.ts >= since)
        # одна сделка могла попасть дважды (например, продажа на Fragment видна и в MarketApp)
        unique = {}
        for s in sales:
            unique.setdefault((s.number, round(s.price, 2), s.ts // 600) if s.number else id(s), s)
        summary = SalesSummary(sorted(unique.values(), key=lambda s: -s.ts), failed, missing)
        if not failed:
            self._cache[key] = (time.time(), summary)
        return summary

    # ---------- Telegram: оценка конкретного подарка ----------
    async def ton_rate(self, currency: str) -> float | None:
        """Сколько стоит 1 TON в валюте (USD, EUR…)."""
        cur = currency.upper()
        hit = self._rates.get(cur)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        resp = await self.http.get(TON_RATES, params={"tokens": "ton", "currencies": cur.lower()})
        resp.raise_for_status()
        prices = resp.json().get("rates", {}).get("TON", {}).get("prices", {})
        rate = prices.get(cur)
        if rate:
            self._rates[cur] = (time.time(), float(rate))
        return rate

    async def gift_value(self, slug: str) -> dict | None:
        """Официальная оценка Telegram: последняя продажа этого подарка, средняя цена продажи и флор такого типа (в TON)."""
        if not USER_CLIENT_ENABLED:
            return None
        hit = self._values.get(slug)
        if hit and time.time() - hit[0] < VALUE_TTL:
            return hit[1]
        from telethon import functions

        from user_client import get_client

        client = await get_client()
        info = await client(functions.payments.GetUniqueStarGiftValueInfoRequest(slug=slug))
        rate = await self.ton_rate(info.currency)
        if not rate:
            return None
        # цены в минимальных единицах валюты (центы); у большинства валют 2 знака после запятой
        k = 100 if info.currency.upper() not in ("JPY", "KRW") else 1

        def ton(v):
            return round(v / k / rate, 2) if v else None

        result = {
            "value": ton(info.value),
            "value_is_average": bool(info.value_is_average),
            "last_sale": ton(info.last_sale_price),
            "last_sale_ts": int(info.last_sale_date.timestamp()) if info.last_sale_date else None,
            "last_sale_on_fragment": bool(info.last_sale_on_fragment),
            "average": ton(info.average_price),
            "floor": ton(info.floor_price),
            "listed": info.listed_count,
        }
        self._values[slug] = (time.time(), result)
        return result


def sale_to_dict(s: Sale) -> dict:
    return asdict(s)
