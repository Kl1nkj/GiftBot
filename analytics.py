"""Анализ выгодности лота для перепродажи.

С чем сравниваем:
  • стакан той же модели прямо сейчас — самые дешёвые другие лоты на Portals, Tonnel и Fragment
    (тот же подарок, выставленный на нескольких площадках, из сравнения исключается);
  • стакан той же связки «модель + фон» — самые близкие аналоги;
  • редкость и флоры модели, фона и символа внутри коллекции (Portals) — из них видно,
    почему одни такие же подарки стоят дороже других (редкий фон, символ, монохром, номер);
  • «обычная» цена — медиана цен, по которым модель выставляли последние 7 дней
    (история собирается самим ботом из всего, что он видит на площадках);
  • коллекция на Portals — флор, средняя цена продажи и число продаж за 24 часа (ликвидность);
  • реальные продажи модели за 30 дней (Portals, Tonnel, блокчейн через MarketApp) и оценка Telegram
    для самого подарка — см. sales.py. Если сделок хватает, перепродажа считается не дороже их медианы.

Перепродажа считается осторожно: по флору модели (а не по дорогой связке), на шаг дешевле
конкурента, а если у флора толпа — ещё дешевле; минус комиссия площадки и перевод между площадками."""
import asyncio
import html
import json
import logging
import re
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime

from curl_cffi.requests import AsyncSession

from config import MARKETAPP_API_TOKEN, SELL_FEES, TRANSFER_COST_TON
from models import MARKETS, Listing, is_ton, lot_url, number_pattern, norm, portals_collection_url, slugify, strip_rarity
from sales import SalesHistory, SalesSummary, sale_to_dict
from storage import Storage

log = logging.getLogger("analytics")

PORTALS = "https://portal-market.com/api"
PORTALS_HEADERS = {"Origin": "https://portal-market.com", "Referer": "https://portal-market.com/"}
TONNEL = "https://gifts2.tonnel.network/api/pageGifts"
TONNEL_HEADERS = {"Origin": "https://market.tonnel.network", "Referer": "https://market.tonnel.network/"}
FRAGMENT = "https://fragment.com/gifts"
LADDER_MARKETS = ("portals", "tonnel", "fragment") + (("marketapp",) if MARKETAPP_API_TOKEN else ())
MARKETAPP = "https://api.marketapp.ws/v1"

# Реальные продажи: со скольких сделок им верим больше, чем ценам выставления
MIN_SALES = 3

_FR_ITEM_RE = re.compile(
    r'<a href="/gift/(?P<id>[a-z0-9]+-(?P<num>\d+))[^"]*" class="tm-grid-item">(?P<body>.*?)</a>', re.S
)
_FR_PRICE_RE = re.compile(r'icon-ton">([\d,.]+)<')
_FR_CHECKED_RE = re.compile(
    r'data-value="([^"]+)">\s*<label class="tm-main-filters-checkbox js-attribute-checkbox">\s*'
    r'<input type="checkbox" class="checkbox" value="1" checked'
)

PORTALS_GAP = 0.5  # не чаще 2 запросов в секунду, иначе Portals отвечает 429
LADDER_TTL = 90  # стакан модели меняется быстро
COLLECTIONS_TTL = 300
ATTRS_TTL = 180
HISTORY_DAYS = 7
MIN_HISTORY = 5  # меньше наблюдений — «обычную» цену не показываем

# Ликвидность коллекции по числу продаж за сутки на Portals
LIQUID_HIGH = 100
LIQUID_MID = 20

# Толпа у флора: столько конкурентов в пределах +3% — встать первым «на цент дешевле» не выйдет
CROWD_SIZE = 5
CROWD_UNDERCUT = 0.02

# Лот, который висит дольше этого, — цена, по которой не покупают; на него как на рынок не опираемся
STALE_DAYS = 7
# Флор «одинокий», если следующий лот дороже него больше чем на столько — рынок модели редкий
THIN_GAP = 0.25

# Ориентир доплаты за красивый номер (верхняя граница оценки, не идёт в расчёт прибыли)
NUMBER_PREMIUM = {3: 1.5, 2: 1.2, 1: 1.05}

# Признак редкий, если он в самой редкой четверти своей коллекции (у разных коллекций разная шкала)
RARE_QUANTILE = 0.25
# Во сколько раз флор признака выше флора коллекции, чтобы назвать его «дорогим»
PREMIUM_RATIO = 1.3

# Цветовые семейства для поиска «монохрома» по названиям (модель/символ того же цвета, что и фон)
COLOR_FAMILIES = {
    "чёрный": ("black", "onyx", "obsidian", "raven", "ink", "shadow", "midnight", "noir", "dark"),
    "белый": ("white", "ivory", "pearl", "snow", "milk", "frost"),
    "красный": ("red", "ruby", "crimson", "cherry", "scarlet", "carmine", "burgundy", "wine", "blood"),
    "розовый": ("pink", "rose", "fuchsia", "magenta", "bubblegum", "flamingo", "sakura", "raspberry"),
    "оранжевый": ("orange", "tangerine", "amber", "copper", "carrot", "persimmon", "pumpkin", "apricot"),
    "жёлтый": ("yellow", "lemon", "gold", "golden", "banana", "mustard", "sunny", "honey"),
    "зелёный": ("green", "emerald", "mint", "lime", "olive", "jade", "malachite", "pine", "khaki", "celtic"),
    "синий": ("blue", "azure", "sapphire", "navy", "sky", "cobalt", "cyan", "aquamarine", "ocean", "teal"),
    "фиолетовый": ("purple", "violet", "lavender", "plum", "grape", "orchid", "indigo", "amethyst", "lilac"),
    "коричневый": ("brown", "chocolate", "chestnut", "coffee", "caramel", "mocha", "cinnamon", "rosewood"),
    "серый": ("grey", "gray", "silver", "steel", "platinum", "battleship", "gunmetal", "ash"),
}


def color_family(name: str | None) -> str | None:
    words = set(re.findall(r"[a-z]+", (name or "").lower()))
    for family, keys in COLOR_FAMILIES.items():
        if words & set(keys):
            return family
    return None


@dataclass
class Offer:
    market: str
    item_id: str
    price: float
    url: str | None = None
    number: int | None = None
    age_days: float | None = None  # сколько дней висит (Portals отдаёт дату выставления)


def _age_days(iso: str | None) -> float | None:
    try:
        return round((time.time() - datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()) / 86400, 1)
    except (AttributeError, ValueError):
        return None


@dataclass
class Analysis:
    ok: bool = False
    verdict: str = "unknown"  # buy | maybe | skip | unknown
    reasons: list[str] = field(default_factory=list)
    # рынок модели прямо сейчас (без самого лота)
    model_floor: float | None = None
    floor_market: str | None = None
    market_floors: dict = field(default_factory=dict)  # самая низкая цена модели на каждой площадке
    near_floor: int = 0  # сколько конкурентов в пределах +10% от флора
    crowd: int = 0  # сколько конкурентов в пределах +3% от флора
    ladder: list[dict] = field(default_factory=list)
    floor_age: float | None = None  # сколько дней висит самый дешёвый конкурент
    thin: bool = False  # мало лотов у флора — цена конкурента может быть просто «хотелкой»
    still_listed: bool | None = None  # лот ещё в продаже (если нашли его в стакане)
    partial: list[str] = field(default_factory=list)  # площадки, которые не ответили — оценка неполная
    # та же связка «модель + фон»
    combo_floor: float | None = None
    combo_count: int = 0
    combo_ladder: list[dict] = field(default_factory=list)
    # признаки: редкость в % и флоры внутри коллекции
    rarity_model: float | None = None
    rarity_backdrop: float | None = None
    rarity_symbol: float | None = None
    floor_backdrop: float | None = None
    floor_symbol: float | None = None
    value_floor: float | None = None  # нижняя граница цены с учётом всех признаков
    upside_profit: float | None = None  # прибыль, если продавать по цене признаков (дольше)
    features: list[str] = field(default_factory=list)  # «почему дороже»
    number_tier: int = 0  # насколько красивый номер: 0 — обычный … 3 — топ (models.number_pattern)
    # класс лота: top | number | rare | common и за что
    grade: str = "common"
    grade_why: list[str] = field(default_factory=list)
    # оценка рынка — сколько лот стоит по данным, а не сколько за него просят
    fair: float | None = None
    fair_high: float | None = None  # сколько можно просить с учётом номера (ориентир, продаётся дольше)
    fair_basis: str | None = None
    fair_discount: float | None = None  # насколько лот дешевле оценки
    combo_sales_n: int = 0  # сделки с той же моделью и тем же фоном
    combo_sales_median: float | None = None
    # реальные продажи модели за 30 дней
    sales_n: int = 0
    sales_median: float | None = None
    sales_min: float | None = None
    sales_max: float | None = None
    discount_sales: float | None = None  # насколько лот дешевле медианы реальных сделок
    sales_markets: dict = field(default_factory=dict)  # площадка → сколько сделок
    sales_list: list[dict] = field(default_factory=list)  # последние сделки
    sales_missing: list[str] = field(default_factory=list)  # нет входа/токена
    sales_failed: list[str] = field(default_factory=list)
    tg: dict | None = None  # оценка Telegram для этого подарка: last_sale, average, floor (TON)
    # история
    usual: float | None = None
    usual_n: int = 0
    # коллекция
    coll_floor: float | None = None
    coll_avg_sale: float | None = None
    coll_sales24: int | None = None
    coll_listed: int | None = None
    coll_url: str | None = None  # коллекция на Portals
    liquidity: str | None = None  # high | mid | low
    # перепродажа
    discount: float | None = None  # насколько дешевле флора модели, доля
    discount_usual: float | None = None
    sell_price: float | None = None
    sell_market: str | None = None
    sell_fee: float | None = None
    transfer: float = 0.0
    net: float | None = None
    profit: float | None = None
    roi: float | None = None
    # пессимистичный сценарий: продать по флору коллекции (любая модель уходит не дешевле него)
    safe_net: float | None = None
    safe_profit: float | None = None
    confidence: str | None = None  # high | mid | low — насколько можно верить оценке
    ts: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class _Cache:
    def __init__(self, ttl: float):
        self.ttl = ttl
        self.data: dict = {}

    def get(self, key):
        hit = self.data.get(key)
        if hit and time.time() - hit[0] < self.ttl:
            return hit[1]
        return None

    def put(self, key, value):
        if len(self.data) > 2000:
            self.data.clear()
        self.data[key] = (time.time(), value)
        return value


def _undercut(price: float) -> float:
    """Цена, чтобы встать первым в стакане: на шаг дешевле конкурента."""
    step = max(0.01, round(price * 0.005, 2))
    return round(price - step, 2)


def _attr(table: dict, name: str | None) -> dict:
    return table.get(norm(name), {}) if name else {}


class Analyzer:
    def __init__(self, storage: Storage):
        self.storage = storage
        self.http = AsyncSession(impersonate="chrome", timeout=20)
        self._ladders = _Cache(LADDER_TTL)
        self._collections = _Cache(COLLECTIONS_TTL)
        self._attrs = _Cache(ATTRS_TTL)
        self._portals_gate = asyncio.Lock()
        self._portals_last = 0.0
        self.sales = SalesHistory(self.http, self._portals_get)

    # ---------- настройки режима «охотник за выгодой» ----------
    def deal_settings(self) -> dict:
        s = self.storage
        return {
            "enabled": s.get_setting("deals_enabled", "1") == "1",
            "min_roi": float(s.get_setting("deals_min_roi", "15")),
            "min_profit": float(s.get_setting("deals_min_profit", "1")),
            "min_discount": float(s.get_setting("deals_min_discount", "7")),
            "max_price": float(v) if (v := s.get_setting("deals_max_price", "")) else None,
            "only_liquid": s.get_setting("deals_only_liquid", "1") == "1",
            # присылать красивые номера, выставленные почти по цене обычных
            "numbers": s.get_setting("deals_numbers", "1") == "1",
        }

    def save_deal_settings(self, data: dict) -> dict:
        s = self.storage
        if "enabled" in data:
            s.set_setting("deals_enabled", "1" if data["enabled"] else "0")
        for key in ("min_roi", "min_profit", "min_discount"):
            if data.get(key) not in (None, ""):
                s.set_setting(f"deals_{key}", str(max(0.0, float(data[key]))))
        if "max_price" in data:
            v = data["max_price"]
            s.set_setting("deals_max_price", "" if v in (None, "") else str(float(v)))
        if "only_liquid" in data:
            s.set_setting("deals_only_liquid", "1" if data["only_liquid"] else "0")
        if "numbers" in data:
            s.set_setting("deals_numbers", "1" if data["numbers"] else "0")
        return self.deal_settings()

    # ---------- данные ----------
    async def _portals_get(self, path: str, params: dict | None = None, headers: dict | None = None):
        """GET к Portals с паузой между запросами и повтором при 429 (лимит запросов)."""
        for attempt in range(3):
            async with self._portals_gate:
                wait = self._portals_last + PORTALS_GAP - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                resp = await self.http.get(f"{PORTALS}{path}", params=params, headers=headers or PORTALS_HEADERS)
                self._portals_last = time.monotonic()
            if resp.status_code != 429:
                resp.raise_for_status()
                return resp.json()
            await asyncio.sleep(1.5 * (attempt + 1))
        resp.raise_for_status()

    async def collections(self) -> dict[str, dict]:
        """norm(название) → статистика коллекции на Portals."""
        cached = self._collections.get("all")
        if cached is not None:
            return cached
        data = await self._portals_get("/collections", {"limit": 500})
        result = {}
        for c in data.get("collections", []):
            sales = int(c.get("sales_24h_count") or 0)
            volume = float(c.get("day_volume") or 0)
            result[norm(c.get("name"))] = {
                "short": c.get("short_name"),
                "id": c.get("id"),
                "floor": float(c["floor_price"]) if c.get("floor_price") else None,
                "sales24": sales,
                "avg_sale": volume / sales if sales else None,
                "listed": int(c.get("listed_count") or 0),
            }
        return self._collections.put("all", result)

    async def attributes(self, collection: str) -> dict[str, dict]:
        """Модели, фоны и символы коллекции: {"models"|"backdrops"|"symbols": {norm(имя): {floor, rarity}}}.
        Редкость Portals отдаёт сразу в процентах (поле называется per_mille, но это проценты)."""
        key = norm(collection)
        cached = self._attrs.get(key)
        if cached is not None:
            return cached
        info = (await self.collections()).get(key)
        result = {"models": {}, "backdrops": {}, "symbols": {}, "rare": {}}
        if info and info.get("short"):
            data = await self._portals_get("/collections/filters", {"short_names": info["short"]})
            raw = (data.get("collections") or {}).get(info["short"], {})
            for kind in result:
                for x in raw.get(kind) or []:
                    rarity = x.get("rarity_per_mille", x.get("rarityPermille"))
                    result[kind][norm(x.get("name"))] = {
                        "floor": float(x["floor_price"]) if x.get("floor_price") else None,
                        "rarity": float(rarity) if rarity is not None else None,
                    }
            for kind in ("models", "backdrops", "symbols"):
                values = sorted(x["rarity"] for x in result[kind].values() if x["rarity"] is not None)
                if len(values) >= 4:
                    result["rare"][kind] = values[int(len(values) * RARE_QUANTILE)]
        return self._attrs.put(key, result)

    async def _portals_ladder(self, collection: str, model: str, backdrop: str | None) -> list[Offer]:
        params = {
            "offset": 0, "limit": 10, "sort_by": "price asc", "status": "listed",
            "filter_by_collections": collection, "filter_by_models": model,
        }
        if backdrop:
            params["filter_by_backdrops"] = backdrop
        data = await self._portals_get("/nfts/search", params)
        offers = []
        for nft in data.get("results", []):
            attrs = {a.get("type"): a.get("value") for a in nft.get("attributes") or []}
            if norm(attrs.get("model")) != norm(model) or not nft.get("price"):
                continue
            if backdrop and norm(attrs.get("backdrop")) != norm(backdrop):
                continue
            offers.append(Offer(
                "portals", str(nft.get("id")), float(nft["price"]), lot_url("portals", nft.get("id")),
                nft.get("external_collection_number"), _age_days(nft.get("listed_at")),
            ))
        return offers

    async def _tonnel_ladder(self, collection: str, model: str, backdrop: str | None) -> list[Offer]:
        flt = {
            "price": {"$exists": True}, "buyer": {"$exists": False}, "asset": "TON",
            "gift_name": collection, "model": {"$regex": "^" + re.escape(model) + r" \("},
        }
        if backdrop:
            flt["backdrop"] = {"$regex": "^" + re.escape(backdrop) + r" \("}
        body = {
            "page": 1, "limit": 10, "sort": json.dumps({"price": 1, "gift_id": -1}),
            "filter": json.dumps(flt), "ref": 0, "price_range": None, "user_auth": "",
        }
        resp = await self.http.post(TONNEL, json=body, headers=TONNEL_HEADERS)
        resp.raise_for_status()
        return [
            Offer("tonnel", str(g.get("gift_id")), float(g["price"]), lot_url("tonnel", g.get("gift_id")), g.get("gift_num"))
            for g in resp.json()
            if g.get("price") is not None
            and norm(strip_rarity(g.get("model"))) == norm(model)
            and (not backdrop or norm(strip_rarity(g.get("backdrop"))) == norm(backdrop))
        ]

    async def _fragment_ladder(self, collection: str, model: str, backdrop: str | None) -> list[Offer]:
        params = {"sort": "price_asc", "filter": "sale", "attr[Model]": json.dumps([model])}
        if backdrop:
            params["attr[Backdrop]"] = json.dumps([backdrop])
        resp = await self.http.get(f"{FRAGMENT}/{slugify(collection)}", params=params)
        if resp.status_code == 404:  # коллекции нет на Fragment
            return []
        resp.raise_for_status()
        text = resp.text

        # незнакомое значение фильтра Fragment молча игнорирует и отдаёт всю коллекцию —
        # проверяем, что отмечена ровно наша модель (и фон)
        def checked(field_name: str) -> list[str]:
            start = text.find(f'data-field="attr[{field_name}]"')
            if start < 0:
                return []
            end = text.find('data-field="attr[', start + 10)
            return [norm(html.unescape(v)) for v in _FR_CHECKED_RE.findall(text[start:end if end > 0 else None])]

        if checked("Model") != [norm(model)] or (backdrop and checked("Backdrop") != [norm(backdrop)]):
            return []
        offers = []
        for m in _FR_ITEM_RE.finditer(text):
            body = m.group("body")
            price = _FR_PRICE_RE.search(body)
            if "tm-status-avail" not in body or not price:  # аукционы пропускаем: там не цена покупки
                continue
            offers.append(Offer(
                "fragment", m.group("id"), float(price.group(1).replace(",", "")),
                lot_url("fragment", m.group("id")), int(m.group("num")),
            ))
            if len(offers) >= 10:
                break
        return offers

    async def _marketapp_ladder(self, collection: str, model: str, backdrop: str | None) -> list[Offer]:
        """Лоты в блокчейне (MarketApp, GetGems, Fragment-контракты) — нужен MARKETAPP_API_TOKEN."""
        address = await self.sales.marketapp_collection(collection)
        if not address:
            return []
        params = {"collection_address": address, "model": model, "sort_by": "min_bid_asc", "limit": 10}
        if backdrop:
            params["backdrop"] = backdrop
        resp = await self.http.get(f"{MARKETAPP}/gifts/onsale/", params=params,
                                   headers={"Authorization": MARKETAPP_API_TOKEN})
        resp.raise_for_status()
        offers = []
        for nft in resp.json().get("items", []):
            attrs = {str(x.get("trait_type", "")).lower(): x.get("value") for x in nft.get("attributes") or []}
            if norm(attrs.get("model")) != norm(model) or not is_ton(nft.get("currency")) or not nft.get("min_bid"):
                continue
            if backdrop and norm(attrs.get("backdrop")) != norm(backdrop):
                continue
            listed = nft.get("listed_at")
            offers.append(Offer(
                "marketapp", nft["address"], float(nft["min_bid"]), None, nft.get("item_num"),
                round((time.time() - listed) / 86400, 1) if listed else None,
            ))
        return offers

    async def ladder(
        self, collection: str, model: str, backdrop: str | None = None, fresh: bool = False
    ) -> tuple[list[Offer], list[str]]:
        """Самые дешёвые лоты модели (или связки модель+фон) на всех площадках и площадки, которые не ответили."""
        key = (norm(collection), norm(model), norm(backdrop))
        if not fresh and (cached := self._ladders.get(key)) is not None:
            return cached, []
        results = await asyncio.gather(
            self._portals_ladder(collection, model, backdrop),
            self._tonnel_ladder(collection, model, backdrop),
            self._fragment_ladder(collection, model, backdrop),
            *([self._marketapp_ladder(collection, model, backdrop)] if MARKETAPP_API_TOKEN else []),
            return_exceptions=True,
        )
        offers, failed = [], []
        for market, r in zip(LADDER_MARKETS, results):
            if isinstance(r, Exception):
                log.warning("стакан %s / %s / %s на %s: %s", collection, model, backdrop, market, r)
                failed.append(market)
            else:
                offers.extend(r)
        offers.sort(key=lambda o: o.price)
        # блокчейн-лот виден и на своей площадке, и в MarketApp — оставляем одну (самую дешёвую) запись
        seen_numbers: set[int] = set()
        unique = []
        for o in offers:
            if o.number is not None and o.number in seen_numbers:
                continue
            if o.number is not None:
                seen_numbers.add(o.number)
            unique.append(o)
        offers = unique
        if not failed:  # неполный стакан не кэшируем, чтобы следующий анализ запросил заново
            self._ladders.put(key, offers)
        return offers, failed

    async def model_overview(self, collection: str, model: str) -> tuple[list[Offer], list[str]]:
        """Все лоты модели на площадках (до 10 самых дешёвых с каждой) — для экрана «вся модель»."""
        return await self.ladder(collection, model, fresh=True)

    def price_series(self, collection: str, model: str, days: int = 14) -> list[tuple[int, str, float]]:
        return self.storage.price_series(norm(collection), norm(model), days * 86400)

    # ---------- история цен ----------
    def record(self, items: list[Listing]) -> None:
        """Запоминает цены всех увиденных лотов — из них считается «обычная» цена модели."""
        rows = [
            (norm(i.collection), norm(i.model), i.market, i.price)
            for i in items
            if i.model and i.price and is_ton(i.currency)
        ]
        self.storage.add_price_obs(rows)

    def usual_price(self, collection: str, model: str) -> tuple[float | None, int]:
        prices = self.storage.price_obs(norm(collection), norm(model), HISTORY_DAYS * 86400)
        if len(prices) < MIN_HISTORY:
            return None, len(prices)
        return statistics.median(prices), len(prices)

    # ---------- быстрый отсев для режима «охотник» ----------
    async def is_candidate(self, item: Listing, settings: dict) -> bool:
        """Дёшево (из кэша) проверяет, есть ли смысл делать полный анализ."""
        if not item.price or not is_ton(item.currency) or not item.model:
            return False
        if settings["max_price"] is not None and item.price > settings["max_price"]:
            return False
        try:
            ref = _attr((await self.attributes(item.collection))["models"], item.model).get("floor")
        except Exception as e:
            log.debug("attributes: %s", e)
            ref = None
        if ref is None:
            ref, _ = self.usual_price(item.collection, item.model)
        if ref is None:
            return False
        # не дороже флора модели: только такие лоты можно перепродать с наценкой;
        # красивому номеру даём запас 5% — за номер доплачивают сверх цены модели
        if settings.get("numbers") and number_pattern(item.number)[0] >= 2:
            return item.price <= ref * 1.05
        return item.price <= ref

    # ---------- полный анализ ----------
    async def analyze(self, item: Listing, fresh: bool = False, check_listed: bool = False) -> Analysis:
        """fresh — стакан без кэша; check_listed — проверить, что лот ещё продаётся
        (только при ручном пересчёте: свежий лот может ещё не попасть в поиск площадки)."""
        a = Analysis(ts=int(time.time()))
        if not item.price or not is_ton(item.currency):
            a.reasons.append("цена не в TON — не сравниваю")
            return a
        if not item.model:
            a.reasons.append("модель неизвестна")
            return a

        try:
            coll = (await self.collections()).get(norm(item.collection))
        except Exception as e:
            log.debug("collections: %s", e)
            coll = None
        if coll:
            a.coll_floor, a.coll_avg_sale = coll["floor"], coll["avg_sale"]
            a.coll_url = portals_collection_url(coll["id"]) if coll.get("id") else None
            a.coll_sales24, a.coll_listed = coll["sales24"], coll["listed"]
            a.liquidity = "high" if coll["sales24"] >= LIQUID_HIGH else "mid" if coll["sales24"] >= LIQUID_MID else "low"

        a.usual, a.usual_n = self.usual_price(item.collection, item.model)

        (offers, failed), (combo, combo_failed), sales, tg = await asyncio.gather(
            self.ladder(item.collection, item.model, fresh=fresh),
            self.ladder(item.collection, item.model, item.backdrop, fresh=fresh) if item.backdrop else _empty(),
            self._model_sales(item.collection, item.model, fresh),
            self._gift_value(item),
        )
        a.partial = failed
        self._apply_sales(item, a, sales)
        a.tg = tg

        def is_self(o: Offer) -> bool:
            # сам лот и тот же подарок, выставленный на другой площадке
            return (o.market == item.market and o.item_id == str(item.item_id)) or (
                item.number is not None and o.number == item.number
            )

        if check_listed and item.market in LADDER_MARKETS:
            a.still_listed = any(o.market == item.market and o.item_id == str(item.item_id) for o in offers)
        others = [o for o in offers if not is_self(o)]
        combo_others = [o for o in combo if not is_self(o)]
        a.ladder = [asdict(o) for o in others[:10]]
        for o in others:  # others отсортирован по цене — первая встреча площадки и есть её флор
            a.market_floors.setdefault(o.market, round(o.price, 3))
        a.combo_ladder = [asdict(o) for o in combo_others[:5]]
        if combo_others:
            a.combo_floor, a.combo_count = combo_others[0].price, len(combo_others)

        if others:
            a.model_floor, a.floor_market = others[0].price, others[0].market
            a.near_floor = sum(1 for o in others if o.price <= a.model_floor * 1.10)
            a.crowd = sum(1 for o in others if o.price <= a.model_floor * 1.03)
            a.floor_age = others[0].age_days
            a.thin = len(others) < 2 or others[1].price > a.model_floor * (1 + THIN_GAP)
            a.discount = 1 - item.price / a.model_floor
        if a.usual:
            a.discount_usual = 1 - item.price / a.usual

        await self._features(item, a)

        # за сколько реально продать: встать первым в стакане модели; у толпы — заметно дешевле флора
        target = None
        if a.model_floor:
            target = _undercut(a.model_floor)
            if a.crowd >= CROWD_SIZE:
                target = round(a.model_floor * (1 - CROWD_UNDERCUT), 2)
        if a.usual and a.usual_n >= MIN_HISTORY * 2:
            target = min(target, round(a.usual, 2)) if target else round(a.usual, 2)
        # реальные сделки важнее цен выставления: дороже их медианы модель не продать
        if a.sales_n >= MIN_SALES:
            target = min(target, round(a.sales_median, 2)) if target else round(a.sales_median, 2)
        if target is None:
            a.reasons.append("других лотов и сделок с этой моделью нет — не с чем сравнить")
            return a

        # где продавать: площадка, где модель торгуется, с наименьшей потерей на комиссии и переводе
        markets = {o.market for o in others} or {item.market}
        best = None
        for m in markets:
            fee = SELL_FEES.get(m, 0.05)
            transfer = 0.0 if m == item.market else TRANSFER_COST_TON
            net = target * (1 - fee) - transfer
            if best is None or net > best[0]:
                best = (net, m, fee, transfer)
        a.net, a.sell_market, a.sell_fee, a.transfer = round(best[0], 3), best[1], best[2], best[3]
        a.sell_price = target
        a.profit = round(a.net - item.price, 3)
        a.roi = a.profit / item.price

        self._fair_value(item, a)

        if a.value_floor and a.value_floor > target * 1.05:
            a.upside_profit = round(_undercut(a.value_floor) * (1 - best[2]) - best[3] - item.price, 3)

        # флор коллекции на Portals мог быть самим этим лотом — тогда он не опора
        coll_floor = a.coll_floor
        if coll_floor and item.market == "portals" and abs(coll_floor - item.price) < 0.005:
            coll_floor = None
        if coll_floor:
            fee = SELL_FEES.get("portals", 0.05)
            transfer = 0.0 if item.market == "portals" else TRANSFER_COST_TON
            a.safe_net = round(_undercut(coll_floor) * (1 - fee) - transfer, 3)
            a.safe_profit = round(a.safe_net - item.price, 3)

        agrees = (
            not a.usual or a.usual_n < MIN_HISTORY or abs(a.model_floor / a.usual - 1) < 0.25
            if a.model_floor else True
        )
        stale = a.floor_age is not None and a.floor_age > STALE_DAYS
        if a.sales_n >= MIN_SALES * 2:
            a.confidence = "high"  # цена перепродажи опирается на реальные сделки
        elif a.sales_n >= MIN_SALES:
            a.confidence = "mid"
        elif stale or a.thin:
            a.confidence = "low"
        elif a.near_floor >= 3 and agrees:
            a.confidence = "high"
        elif len(others) >= 2:
            a.confidence = "mid"
        else:
            a.confidence = "low"
        a.ok = True
        a.verdict, a.reasons = self._verdict(a)
        return a

    async def _model_sales(self, collection: str, model: str, fresh: bool) -> SalesSummary | None:
        try:
            return await self.sales.model_sales(collection, model, fresh=fresh)
        except Exception as e:
            log.warning("продажи %s / %s: %s", collection, model, e)
            return None

    async def _gift_value(self, item: Listing) -> dict | None:
        if item.number is None:
            return None
        try:
            return await self.sales.gift_value(re.sub(r"[^A-Za-z0-9]", "", item.collection) + f"-{item.number}")
        except Exception as e:
            log.debug("оценка Telegram %s: %s", item.key, e)
            return None

    @staticmethod
    def _apply_sales(item: Listing, a: Analysis, sales: SalesSummary | None) -> None:
        if sales is None:
            return
        a.sales_missing, a.sales_failed = sales.missing, sales.failed
        # сам этот подарок мог продаваться раньше — это тоже сделка с моделью, оставляем
        recent = sales.sales[:20]  # медиана по последним сделкам — рынок мог сдвинуться за месяц
        if not recent:
            return
        prices = sorted(s.price for s in recent)
        a.sales_n = len(sales.sales)
        a.sales_median = round(statistics.median(prices), 3)
        a.sales_min, a.sales_max = prices[0], prices[-1]
        for s in sales.sales:
            a.sales_markets[s.market] = a.sales_markets.get(s.market, 0) + 1
        a.sales_list = [sale_to_dict(s) for s in sales.sales[:10]]
        a.discount_sales = 1 - item.price / a.sales_median
        same = [s.price for s in sales.sales if item.backdrop and norm(s.backdrop) == norm(item.backdrop)]
        if same:
            a.combo_sales_n, a.combo_sales_median = len(same), round(statistics.median(same), 3)

    async def _features(self, item: Listing, a: Analysis) -> None:
        """Объясняет цену: редкость модели/фона/символа, дорогие фон и символ, монохром, связка, номер."""
        try:
            attrs = await self.attributes(item.collection)
        except Exception as e:
            log.debug("attributes: %s", e)
            attrs = {"models": {}, "backdrops": {}, "symbols": {}, "rare": {}}
        m = _attr(attrs["models"], item.model)
        b = _attr(attrs["backdrops"], item.backdrop)
        s = _attr(attrs["symbols"], item.symbol)
        a.rarity_model, a.rarity_backdrop, a.rarity_symbol = m.get("rarity"), b.get("rarity"), s.get("rarity")

        # флор признака мог быть самим этим лотом (если он на Portals) — тогда он ничего не говорит
        def floor_of(x: dict) -> float | None:
            f = x.get("floor")
            if f is None or (item.market == "portals" and abs(f - item.price) < 0.005):
                return None
            return f

        a.floor_backdrop, a.floor_symbol = floor_of(b), floor_of(s)
        coll_floor = a.coll_floor or 0
        f = a.features

        rare = attrs.get("rare", {})

        def is_rare(kind: str, value: float | None) -> bool:
            return value is not None and kind in rare and value <= rare[kind]

        why = a.grade_why  # короткие метки для класса лота
        if is_rare("models", a.rarity_model):
            f.append(f"редкая модель {item.model} — {a.rarity_model:g}% выпуска")
            why.append("редкая модель")
        if is_rare("backdrops", a.rarity_backdrop):
            f.append(f"редкий фон {item.backdrop} — {a.rarity_backdrop:g}%")
            why.append("редкий фон")
        if is_rare("symbols", a.rarity_symbol):
            f.append(f"редкий символ {item.symbol} — {a.rarity_symbol:g}%")
            why.append("редкий символ")
        if coll_floor and a.floor_backdrop and a.floor_backdrop >= coll_floor * PREMIUM_RATIO:
            f.append(f"фон {item.backdrop} в этой коллекции дешевле {a.floor_backdrop:g} TON не продают "
                     f"(×{a.floor_backdrop / coll_floor:.1f} к флору)")
            why.append("дорогой фон")
        if coll_floor and a.floor_symbol and a.floor_symbol >= coll_floor * PREMIUM_RATIO:
            f.append(f"символ {item.symbol} дешевле {a.floor_symbol:g} TON не продают "
                     f"(×{a.floor_symbol / coll_floor:.1f} к флору)")
            why.append("дорогой символ")

        fam_b = color_family(item.backdrop)
        if fam_b and fam_b in (color_family(item.model), color_family(item.symbol)):
            f.append(f"похоже на монохром — {fam_b} фон в цвет модели/символа, такие ценятся выше")
            why.append("монохром")
        if a.combo_floor and a.combo_count >= 2 and a.model_floor and a.combo_floor >= a.model_floor * 1.15:
            f.append(f"с этим фоном модель продают от {a.combo_floor:g} TON, а в среднем от {a.model_floor:g} — "
                     f"сочетание ценится дороже (часто это монохром или удачные цвета)")
            why.append("ценное сочетание")

        a.number_tier, label = number_pattern(item.number)
        if label:
            f.append(label + (" — за такие часто доплачивают" if a.number_tier >= 2 else ""))
        if a.number_tier >= 2:
            why.insert(0, f"номер #{item.number}")

        # класс лота: номер весит больше всего, остальные признаки добавляют по очку
        points = {3: 3, 2: 2}.get(a.number_tier, 0) + len(why) - (a.number_tier >= 2)
        if a.number_tier == 3 or points >= 4 or (a.number_tier == 2 and points >= 3):
            a.grade = "top"
        elif a.number_tier >= 2:
            a.grade = "number"
        elif why:
            a.grade = "rare"

        # нижняя граница цены: подарок с этими признаками не бывает дешевле самого дешёвого из них
        # одиночный лот связки — часто просто завышенная цена, на него не опираемся
        combo = a.combo_floor if a.combo_count >= 2 else None
        # висящий годами или одинокий лот — не цена рынка, на него не опираемся
        stale = a.floor_age is not None and a.floor_age > STALE_DAYS
        model_floor = None if stale or a.thin else a.model_floor
        bounds = [x for x in (model_floor, combo, a.floor_backdrop, a.floor_symbol) if x]
        a.value_floor = max(bounds) if bounds else None
        if a.value_floor and a.sales_n >= MIN_SALES:  # дороже самой дорогой сделки не закладываем
            a.value_floor = min(a.value_floor, a.sales_max)

    @staticmethod
    def _fair_value(item: Listing, a: Analysis) -> None:
        """Оценка рынка: сделки → цена в продаже → история; поднимается до цены связки «модель+фон»
        и дорогих фона/символа. Верхняя граница — с доплатой за красивый номер (ориентир)."""
        stale = a.floor_age is not None and a.floor_age > STALE_DAYS
        if a.sales_n >= MIN_SALES:
            base, a.fair_basis = a.sales_median, f"по {a.sales_n} сделкам за 30 дн."
        elif a.model_floor and not stale and not a.thin:
            base, a.fair_basis = a.model_floor, "по самой дешёвой цене в продаже (сделок мало)"
        elif a.usual:
            base, a.fair_basis = a.usual, "по ценам выставления за 7 дн."
        else:
            return
        if a.combo_sales_n >= 2 and a.combo_sales_median > base:
            base, a.fair_basis = a.combo_sales_median, f"по {a.combo_sales_n} сделкам с этим же фоном"
        elif a.value_floor and a.value_floor > base:
            base = a.value_floor
            a.fair_basis += ", с учётом фона/символа"
        a.fair = round(base, 2)
        premium = NUMBER_PREMIUM.get(a.number_tier, 1.0)
        if premium > 1:
            a.fair_high = round(a.fair * premium, 2)
        a.fair_discount = 1 - item.price / a.fair

    def _verdict(self, a: Analysis) -> tuple[str, list[str]]:
        s = self.deal_settings()
        reasons = []
        if a.profit <= 0:
            if a.sales_n >= MIN_SALES and a.discount_sales is not None and a.discount_sales < 0.05:
                reasons.append(f"реально модель покупают за ~{_ton(a.sales_median)} TON — "
                               f"по сделкам этот лот не дешевле рынка")
            elif a.discount is not None and a.discount < 0:
                reasons.append("выставлен дороже рынка модели — перепродать с прибылью не выйдет")
            else:
                reasons.append("после комиссии в минус или в ноль — по сути цена рынка")
            return "skip", reasons
        good = True
        if a.roi * 100 < s["min_roi"]:
            reasons.append(f"доходность {a.roi * 100:.0f}% — ниже порога {s['min_roi']:g}%")
            good = False
        if a.profit < s["min_profit"]:
            reasons.append(f"прибыль {a.profit:.2f} TON — меньше {s['min_profit']:g} TON")
            good = False
        # с реальными сделками запас считаем от них, иначе — от самого дешёвого в продаже
        disc = a.discount_sales if a.sales_n >= MIN_SALES else a.discount
        if disc is not None and disc * 100 < s["min_discount"]:
            reasons.append(f"всего на {disc * 100:.0f}% дешевле рынка — запас слишком маленький")
            good = False
        if a.liquidity == "low":
            reasons.append("коллекция продаётся редко — можно долго ждать покупателя")
            if s["only_liquid"]:
                good = False
        if a.crowd >= CROWD_SIZE:
            reasons.append(f"у флора {a.crowd} конкурентов — цена перепродажи взята на {CROWD_UNDERCUT * 100:g}% ниже флора")
        if a.usual and a.usual_n >= MIN_HISTORY and a.model_floor and a.model_floor < a.usual * 0.85:
            reasons.append("модель сейчас дешевеет — флор ниже обычной цены")
            good = False
        safe = a.safe_profit is not None and a.safe_profit >= 0
        if a.sales_n >= MIN_SALES and a.sell_price is not None and a.model_floor \
                and a.sales_median < a.model_floor * 0.9:
            reasons.append(f"реально модель покупают в среднем за {_ton(a.sales_median)} — "
                           f"дешевле, чем её выставляют; перепродажа посчитана по сделкам")
        if a.floor_age is not None and a.floor_age > STALE_DAYS:
            reasons.append(f"самый дешёвый такой же висит уже {a.floor_age:.0f} дн. — по этой цене его не покупают")
        if a.thin and a.sales_n < MIN_SALES:
            reasons.append("у флора модели почти нет лотов — это цена одного продавца, а не рынка")
        if a.sales_n == 0 and not a.sales_missing and not a.sales_failed:
            reasons.append("за 30 дней эту модель ни разу не купили — продавать придётся долго")
            good = False
        if a.confidence == "low" and not safe:
            if not a.thin and not (a.floor_age and a.floor_age > STALE_DAYS):
                reasons.append("других лотов этой модели почти нет — цена конкурента может быть завышена")
            reasons.append("перепродажа по этой цене не гарантирована, а по флору коллекции — в минус")
            good = False
        if safe:
            reasons.insert(0, "даже по флору коллекции не в минус — риск минимальный")
        if a.still_listed is False:
            reasons.append("лота уже нет в продаже")
            good = False
        if a.partial:
            names = ", ".join(MARKETS.get(m, m) for m in a.partial)
            reasons.append(f"{names} не ответил(а) — оценка неполная, нажмите «Пересчитать»")
            good = False
        return ("buy" if good else "maybe"), reasons

    async def close(self) -> None:
        await self.http.close()


async def _empty() -> tuple[list[Offer], list[str]]:
    return [], []


GRADES = {
    "top": "🏆 Топ-лот",
    "number": "💎 Красивый номер",
    "rare": "✨ Редкий",
    "common": "▫️ Обычный",
}


def grade_line(a: "Analysis") -> str:
    why = f" · {', '.join(a.grade_why)}" if a.grade_why else ""
    return f"<b>{GRADES.get(a.grade, GRADES['common'])}</b>{why}"


VERDICTS = {
    "buy": "🟢 Брать — перепродажа в плюс",
    "maybe": "🟡 Можно, но есть риски",
    "skip": "🔴 Не выгодно",
    "unknown": "⚪ Мало данных для оценки",
}
LIQUIDITY = {"high": "высокая", "mid": "средняя", "low": "низкая"}
CONFIDENCE = {"high": "высокая", "mid": "средняя", "low": "низкая (мало лотов для сравнения)"}


def _ton(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:,.2f}".rstrip("0").rstrip(".").replace(",", " ")


def _pct(v: float) -> str:
    return f"{v * 100:+.0f}%".replace("+-", "−").replace("-", "−")


def format_analysis(item: Listing, a: Analysis) -> str:
    """Блок анализа для уведомления в Telegram (HTML)."""
    lines = ["", "📊 <b>Анализ</b>"]
    if a.fair is not None:
        d = a.fair_discount or 0
        rel = f"лот на <b>{abs(d) * 100:.0f}% {'дешевле' if d > 0 else 'дороже'}</b> оценки" if abs(d) >= 0.01 \
            else "лот стоит ровно по оценке"
        lines.append(f"🧮 <b>Оценка рынка: ~{_ton(a.fair)} TON</b> ({a.fair_basis}) — {rel}")
        if a.fair_high:
            lines.append(f"За номер можно просить до ~{_ton(a.fair_high)} TON (ориентир, продаётся дольше)")
    if a.model_floor is not None:
        cnt = f", рядом с ним ещё {a.near_floor - 1}" if a.near_floor > 1 else ""
        age = f", висит {a.floor_age:.0f} дн." if a.floor_age is not None and a.floor_age >= 1 else ""
        lines.append(
            f"Дешевле всего эта модель сейчас: <b>{_ton(a.model_floor)} TON</b> "
            f"({MARKETS.get(a.floor_market, a.floor_market)}{cnt}{age})"
        )
    if a.market_floors or a.partial:
        parts = [f"{MARKETS.get(m, m)} {_ton(p)}" for m, p in a.market_floors.items()]
        parts += [f"{MARKETS.get(m, m)} нет" for m in LADDER_MARKETS if m not in a.market_floors and m not in a.partial]
        parts += [f"{MARKETS.get(m, m)} не ответил" for m in a.partial]
        lines.append("По площадкам: " + " · ".join(parts))
    if a.sales_n:
        where = ", ".join(f"{MARKETS.get(m, m.capitalize())} {n}" for m, n in a.sales_markets.items())
        last = a.sales_list[0]
        ago = max(0, (time.time() - last["ts"]) / 86400)
        lines.append(
            f"💸 Реально купили за 30 дн.: <b>{a.sales_n}</b> раз ({where}), середина <b>{_ton(a.sales_median)} TON</b>, "
            f"от {_ton(a.sales_min)} до {_ton(a.sales_max)}; последняя — {_ton(last['price'])} "
            f"{'сегодня' if ago < 1 else f'{ago:.0f} дн. назад'}"
        )
    elif not a.sales_n and a.ok and not a.sales_missing and not a.sales_failed:
        lines.append("💸 За 30 дней эту модель ни разу не купили")
    if a.tg and (a.tg.get("last_sale") or a.tg.get("average")):
        parts = []
        if a.tg.get("last_sale"):
            parts.append(f"этот подарок последний раз продан за {_ton(a.tg['last_sale'])} TON")
        if a.tg.get("average"):
            parts.append(f"средняя продажа таких {_ton(a.tg['average'])}")
        lines.append("📱 Telegram: " + ", ".join(parts))
    if a.combo_floor is not None:
        lines.append(f"Та же модель с этим фоном: от {_ton(a.combo_floor)} TON ({a.combo_count} в продаже)")
    if a.combo_sales_n:
        lines.append(f"С этим же фоном купили {a.combo_sales_n} раз, середина {_ton(a.combo_sales_median)} TON")
    if a.usual is not None:
        lines.append(f"Обычно выставляют: ~{_ton(a.usual)} TON (медиана за 7 дн., {a.usual_n} лотов)")
    if a.discount_sales is not None and a.sales_n >= MIN_SALES:
        word = "дешевле" if a.discount_sales > 0 else "дороже"
        lines.append(f"Этот лот на <b>{abs(a.discount_sales) * 100:.0f}% {word}</b> реальных сделок"
                     + (f" (и на {abs(a.discount) * 100:.0f}% {'дешевле' if a.discount > 0 else 'дороже'} "
                        f"самого дешёвого в продаже)" if a.discount is not None else ""))
    elif a.discount is not None:
        word = "дешевле" if a.discount > 0 else "дороже"
        lines.append(f"Этот лот на <b>{abs(a.discount) * 100:.0f}% {word}</b> рынка модели")
    if a.coll_floor is not None:
        parts = [f"флор {_ton(a.coll_floor)}"]
        if a.coll_avg_sale:
            parts.append(f"ср. продажа за 24ч {_ton(a.coll_avg_sale)}")
        if a.coll_sales24 is not None:
            parts.append(f"продаж за 24ч {a.coll_sales24} ({LIQUIDITY.get(a.liquidity, '—')} ликвидность)")
        lines.append("Коллекция: " + ", ".join(parts))
    if a.features:
        lines.append("")
        lines.append("🔎 <b>Почему такая цена</b>")
        lines.extend(f"• {x}" for x in a.features)
    if a.ok:
        lines.append("")
        where = MARKETS.get(a.sell_market, a.sell_market)
        transfer = f" − перевод {_ton(a.transfer)}" if a.transfer else ""
        lines.append(
            f"💡 Перепродать: выставить на {where} за {_ton(a.sell_price)} → "
            f"после комиссии {a.sell_fee * 100:g}%{transfer} ≈ <b>{_ton(a.net)} TON</b>"
        )
        sign = "+" if a.profit >= 0 else "−"
        lines.append(f"Прибыль: <b>{sign}{_ton(abs(a.profit))} TON ({_pct(a.roi)})</b>")
        if a.upside_profit is not None and a.upside_profit > max(a.profit, 0):
            lines.append(
                f"С учётом фона/символа можно выставить от {_ton(_undercut(a.value_floor))} → "
                f"{'+' if a.upside_profit >= 0 else '−'}{_ton(abs(a.upside_profit))} TON (продаваться будет дольше)"
            )
        if a.safe_profit is not None and a.profit > 0:
            sign = "+" if a.safe_profit >= 0 else "−"
            lines.append(f"Если продавать по флору коллекции: {sign}{_ton(abs(a.safe_profit))} TON")
        lines.append(f"Надёжность оценки: {CONFIDENCE.get(a.confidence, '—')}")
    lines.append(f"<b>{VERDICTS[a.verdict]}</b>")
    for r in a.reasons:
        lines.append(f"• {r}")
    if a.ladder:
        short = {"portals": "P", "tonnel": "T", "fragment": "F", "marketapp": "M"}
        links = [
            f'<a href="{o["url"]}">{_ton(o["price"])}</a> {short.get(o["market"], "")}' if o.get("url")
            else f'{_ton(o["price"])} {short.get(o["market"], "")}'
            for o in a.ladder[:5]
        ]
        lines.append("")
        lines.append("🛍 Такие же в продаже (TON, P — Portals, T — Tonnel, F — Fragment, M — MarketApp): " + " · ".join(links))
    return "\n".join(lines)
