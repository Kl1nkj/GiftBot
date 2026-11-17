import asyncio
import html
import logging
import re
import time

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from analytics import Analysis, Analyzer, _undercut, format_analysis_short, grade_line
from config import OWNER_IDS, SELL_FEES, STAR_USD, STARS_PER_TON_FIXED
from models import MARKETS, Listing, is_ton, norm
from sources.base import Source
from sources.fragment import FragmentSource
from sources.getgems import GetGemsSource
from sources.marketapp import MarketAppSource
from sources.mrkt import MrktSource
from sources.portals import PortalsSource
from sources.telegram_market import TelegramMarketSource, set_stars_per_ton
from sources.tonnel import TonnelSource
from storage import GiftFilter, Storage

log = logging.getLogger("monitor")

FETCH_TIMEOUT = 90  # зависший запрос к площадке не должен останавливать её опрос навсегда
REALERT_HOURS = 24  # тот же подарок повторно — только если подешевел заметнее, чем на REALERT_DROP
REALERT_DROP = 0.03
MODEL_ALERTS_PER_HOUR = 3  # охотник: не больше стольких алертов по одной модели в час (волна демпинга)
POSITIONS_EVERY = 20 * 60  # как часто проверять купленные подарки
FLOOR_MOVE = 0.10  # о движении флора купленной модели сообщаем от 10%
CHECK_AFTER_DAYS = 3  # через сколько дней проверять, сбылся ли план алерта


def fmt_price(item: Listing) -> str:
    if item.price_text:
        return item.price_text
    if item.price is None:
        return "—"
    return f"{item.price:,.2f}".rstrip("0").rstrip(".").replace(",", " ") + " TON"


def format_listing(
    item: Listing, analysis: Analysis | None = None, hit_id: int | None = None, deal: bool | str = False
) -> tuple[str, InlineKeyboardMarkup]:
    title = html.escape(item.collection) + (f" #{item.number}" if item.number is not None else "")
    lines = [
        *(["💎 <b>Красивый номер почти по цене обычного</b>"] if deal == "number"
          else ["🔥 <b>Выгодный лот</b>"] if deal else []),
        f"🎁 <b>{title}</b>",
        *([grade_line(analysis)] if analysis is not None else []),
        f"💰 <b>{html.escape(fmt_price(item))}</b>",
        f"🏪 {MARKETS.get(item.market, item.market)}",
    ]
    for label, value in (("Модель", item.model), ("Фон", item.backdrop), ("Символ", item.symbol)):
        if value:
            lines.append(f"{label}: {html.escape(value)}")
    if analysis is not None:
        lines.append(format_analysis_short(item, analysis))
    buttons = []
    if item.url:
        buttons.append(InlineKeyboardButton(text=f"🛒 Лот на {MARKETS.get(item.market, item.market)}", url=item.url))
    if item.nft_link and item.nft_link != item.url:
        buttons.append(InlineKeyboardButton(text="Подарок", url=item.nft_link))
    rows = [buttons] if buttons else []
    if analysis is not None and analysis.coll_url:
        rows.append([InlineKeyboardButton(text="📂 Вся коллекция на Portals", url=analysis.coll_url)])
    if hit_id is not None and item.model:
        rows.append([InlineKeyboardButton(text=f"👀 {item.model}: в продаже, сделки, график", callback_data=f"md:{hit_id}")])
    if hit_id is not None:
        rows.append([
            InlineKeyboardButton(text="📊 Подробно", callback_data=f"fa:{hit_id}"),
            InlineKeyboardButton(text="🔄 Пересчитать", callback_data=f"ra:{hit_id}"),
        ])
        if item.price is not None and is_ton(item.currency):
            rows.append([InlineKeyboardButton(text="✅ Купил — в журнал", callback_data=f"bo:{hit_id}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _t(v: float | None) -> str:
    return "—" if v is None else f"{v:,.2f}".rstrip("0").rstrip(".").replace(",", " ")


def caption_len(text: str) -> int:
    """Длина так, как её считает Telegram: без HTML-разметки."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


def listing_from_hit(hit: dict) -> Listing:
    return Listing(
        market=hit["market"], item_id=str(hit.get("item_id") or ""), collection=hit["collection"],
        number=hit.get("number"), price=hit.get("price"), currency=hit.get("currency") or "TON",
        model=hit.get("model"), backdrop=hit.get("backdrop"), symbol=hit.get("symbol"), url=hit.get("url"),
        price_text=hit.get("price_text"),
    )


class Monitor:
    def __init__(self, bot: Bot, storage: Storage):
        self.bot = bot
        self.storage = storage
        self.sources: list[Source] = [
            TelegramMarketSource(),
            PortalsSource(),
            TonnelSource(),
            MrktSource(),
            FragmentSource(),
            GetGemsSource(),
            MarketAppSource(),
        ]
        self.analyzer = Analyzer(storage)
        self.sent_total = 0
        self.deals_total = 0
        self.skipped_dupes = 0
        self.last_cycle = time.time()  # для /status и сторожа: монитор жив
        self._model_alerts: dict[tuple[str, str], list[float]] = {}

    @property
    def paused(self) -> bool:
        return self.storage.get_setting("paused", "0") == "1"

    def _wanted_collections(self, source: Source, filters: list[GiftFilter]) -> set[str] | None:
        wanted: set[str] = set()
        for f in filters:
            if not f.enabled or (f.markets and source.name not in f.markets):
                continue
            if not f.collections:
                return None
            wanted.update(norm(c) for c in f.collections)
        return wanted

    async def run(self) -> None:
        """Каждая площадка опрашивается в своей задаче: медленная или упавшая не задерживает остальные."""
        await asyncio.gather(self._housekeeping(), self._positions_loop(),
                             *(self._source_loop(s) for s in self.sources))

    async def _housekeeping(self) -> None:
        while True:
            try:
                self.storage.prune_seen()
                self.storage.prune_price_obs()
            except Exception:
                log.exception("чистка базы")
            if not STARS_PER_TON_FIXED:
                try:
                    usd = await self.analyzer.sales.ton_rate("USD")
                    if usd:
                        set_stars_per_ton(round(usd / STAR_USD, 1))
                except Exception as e:
                    log.debug("курс TON: %s", e)
            await asyncio.sleep(3600)

    # ---------- купленные подарки ----------
    async def _positions_loop(self) -> None:
        await asyncio.sleep(60)
        while True:
            try:
                await self.check_positions()
                await self.calibrate()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("проверка купленных подарков")
            await asyncio.sleep(POSITIONS_EVERY)

    async def position_market(self, p: dict) -> dict | None:
        """Текущий рынок модели для купленного подарка: флор без него самого и сколько выйдет чистыми."""
        offers, _ = await self.analyzer.ladder(p["collection"], p["model"])
        others = [o for o in offers if o.number != p.get("number")]
        if not others:
            return None
        mine = next((o for o in offers if p.get("number") is not None and o.number == p["number"]), None)
        floor = others[0]
        sell = _undercut(floor.price)
        net = round(sell * (1 - SELL_FEES.get(floor.market, 0.05)), 3)
        return {"floor": floor.price, "floor_market": floor.market, "sell": sell, "net": net,
                "pnl": round(net - p["cost"], 3), "mine": mine.price if mine else None,
                "mine_market": mine.market if mine else None}

    async def check_positions(self, notify: bool = True) -> None:
        for p in self.storage.list_positions():
            if p.get("status") != "open" or not p.get("model"):
                continue
            try:
                m = await self.position_market(p)
            except Exception as e:
                log.warning("позиция %s: %s", p["id"], e)
                continue
            if not m:
                continue
            msgs = []
            last = p.get("notified_floor")
            if last and abs(m["floor"] / last - 1) >= FLOOR_MOVE:
                msgs.append(f"флор модели {'упал' if m['floor'] < last else 'вырос'}: {_t(last)} → <b>{_t(m['floor'])} TON</b>")
            if last is None or msgs:
                p["notified_floor"] = m["floor"]
            if m["mine"] is not None and m["mine"] > m["floor"] and p.get("undercut_floor") != m["floor"]:
                msgs.append(f"тебя перебили: твой лот за {_t(m['mine'])}, а такой же уже за {_t(m['floor'])} "
                            f"на {MARKETS.get(m['floor_market'], m['floor_market'])}")
                p["undercut_floor"] = m["floor"]
            if m["pnl"] < 0 and not p.get("loss_warned"):
                msgs.append("по текущему флору продажа уже в минус")
                p["loss_warned"] = True
            elif m["pnl"] >= 0:
                p["loss_warned"] = False
            p.update(market_now=m, checked=int(time.time()))
            self.storage.update_position(p["id"], p)
            if msgs and notify:
                title = html.escape(p["collection"]) + (f" #{p['number']}" if p.get("number") is not None else "")
                sign = "+" if m["pnl"] >= 0 else "−"
                text = "\n".join([
                    f"📒 <b>{title}</b> · {html.escape(p['model'])} — куплен за {_t(p['cost'])} TON",
                    *(f"• {x}" for x in msgs),
                    f"💡 Сейчас выставлять за <b>{_t(m['sell'])}</b> на {MARKETS.get(m['floor_market'], m['floor_market'])} "
                    f"→ чистыми {_t(m['net'])} ({sign}{_t(abs(m['pnl']))} TON)",
                ])
                for chat_id in OWNER_IDS:
                    try:
                        await self.bot.send_message(chat_id, text, disable_web_page_preview=True)
                    except Exception as e:
                        log.warning("позиция → %s: %s", chat_id, e)

    async def calibrate(self, limit: int = 15) -> None:
        """Через CHECK_AFTER_DAYS дней после алерта смотрим, держится ли план продажи — так видно,
        насколько охотнику можно верить (итог в /portfolio)."""
        now = time.time()
        done = 0
        for h in self.storage.list_hits(2000, int(now - 30 * 86400)):
            a = h.get("analysis") or {}
            if not h.get("deal") or not a.get("ok") or "check" in h or not h.get("model"):
                continue
            if now - h["ts"] < CHECK_AFTER_DAYS * 86400:
                continue
            try:
                offers, failed = await self.analyzer.ladder(h["collection"], h["model"])
            except Exception as e:
                log.debug("проверка алерта %s: %s", h["id"], e)
                continue
            if failed:
                continue
            others = [o for o in offers if o.number != h.get("number")]
            floor = others[0].price if others else None
            h["check"] = {"ts": int(now), "floor": floor, "plan": a.get("sell_price"),
                          "held": bool(floor and a.get("sell_price") and _undercut(floor) >= a["sell_price"] * 0.97)}
            self.storage.update_hit(h["id"], h)
            done += 1
            if done >= limit:
                break

    async def _source_loop(self, source: Source) -> None:
        while True:
            self.last_cycle = time.time()
            try:
                if source.available and source.next_run <= time.time() and not self.paused:
                    await self._poll(source)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # любая ошибка — в лог и повтор позже, но опрос не умирает
                log.exception("%s: цикл опроса упал", source.name)
                source.last_error = f"{type(e).__name__}: {e}"[:300]
                source.next_run = time.time() + max(source.interval * 3, 60)
            await asyncio.sleep(1)

    async def _poll(self, source: Source) -> None:
        source.next_run = time.time() + source.interval
        filters = [f for f in self.storage.list_filters() if f.enabled and (not f.markets or source.name in f.markets)]
        deals = self.analyzer.deal_settings()
        # история цен и охотник за выгодой нужны по всем лотам, поэтому без фильтров тоже опрашиваем
        wanted = None if deals["enabled"] else self._wanted_collections(source, filters)
        if not deals["enabled"] and source.seeded and (not filters or (wanted is not None and not wanted)):
            return
        try:
            items = await asyncio.wait_for(source.fetch(wanted), FETCH_TIMEOUT)
            source.mark_ok(len(items))
        except Exception as e:  # площадка недоступна или поменяла API — пробуем снова позже
            source.last_error = f"{type(e).__name__}: {e}"[:300] if str(e) else type(e).__name__
            # Telegram просит подождать (FloodWait) — ждём не меньше, иначе рискуем аккаунтом
            flood = getattr(e, "seconds", None)
            source.next_run = time.time() + max(source.interval * 3, 60, (flood or 0) + 5)
            log.warning("%s: %s", source.name, source.last_error)
            return

        unseen = self.storage.filter_unseen([i.key for i in items])
        new_items = [i for i in items if i.key in unseen]
        self.analyzer.record(new_items)
        if not source.seeded:
            # первый опрос — просто запоминаем то, что уже выставлено
            self.storage.mark_seen(unseen)
            source.seeded = True
            return

        for item in new_items:
            await self._handle(source, item, filters, deals)
            self.storage.mark_seen([item.key])  # сразу: если цикл упадёт дальше, этот лот не придёт второй раз
        self.storage.mark_seen([i.key for i in items])

    async def _handle(self, source: Source, item: Listing, filters: list[GiftFilter], deals: dict) -> None:
        if item.extra.get("seed"):
            return
        candidates = [f for f in filters if f.matches_basic(item)]
        if not candidates and not deals["enabled"]:
            return
        if item.needs_details and not candidates and not await self._worth_details(item):
            return
        if item.needs_details:
            try:
                await source.load_details(item)
                self.analyzer.record([item])
            except Exception as e:
                log.warning("%s details: %s", source.name, e)
        try:
            if any(f.matches(item) for f in candidates):
                await self._notify(item, await self._analyze(item))
            elif deals["enabled"] and await self.analyzer.is_candidate(item, deals):
                # для охотника стакан всегда свежий: при волне демпинга кэш показал бы устаревший флор
                analysis = await self._analyze(item, fresh=True)
                if analysis and analysis.verdict == "buy":
                    await self._notify(item, analysis, deal=True)
                elif analysis and deals["numbers"] and self._cheap_number(item, analysis):
                    await self._notify(item, analysis, deal="number")
        except Exception as e:
            log.warning("%s notify: %s", source.name, e)

    @staticmethod
    def _cheap_number(item: Listing, a: Analysis) -> bool:
        """Красивый номер (повтор 183183, 7777, круглый, до #1000), выставленный не дороже обычных таких же:
        запас тут не в цене модели, а в доплате коллекционеров за номер — поэтому шлём отдельным типом."""
        if a.number_tier < 2 or not a.ok or a.still_listed is False:
            return False
        ref = a.sales_median if a.sales_n >= 3 else a.model_floor
        return bool(ref) and item.price <= ref * 1.05

    async def _worth_details(self, item: Listing) -> bool:
        """Для охотника не грузим страницу каждого лота Fragment — только недорогие относительно коллекции."""
        try:
            coll = (await self.analyzer.collections()).get(norm(item.collection))
        except Exception:
            return False
        return bool(coll and coll["floor"] and item.price and item.price <= coll["floor"] * 2)

    async def _analyze(self, item: Listing, fresh: bool = False) -> Analysis | None:
        try:
            return await asyncio.wait_for(self.analyzer.analyze(item, fresh=fresh), 30)
        except Exception as e:
            log.warning("анализ %s: %s", item.key, e)
            return None

    def _is_repeat(self, item: Listing, deal: bool | str) -> bool:
        """Тот же подарок (перецена на копейку или он же на другой площадке) и волна одной модели — не спамим."""
        ident = f"{norm(item.collection)}#{item.number}" if item.number is not None else item.key
        prev = self.storage.last_alert(ident, REALERT_HOURS * 3600)
        if prev is not None and item.price is not None and item.price > prev * (1 - REALERT_DROP):
            return True
        if deal and item.model:
            key = (norm(item.collection), norm(item.model))
            recent = [t for t in self._model_alerts.get(key, []) if time.time() - t < 3600]
            if len(recent) >= MODEL_ALERTS_PER_HOUR:
                return True
            self._model_alerts[key] = recent + [time.time()]
        self.storage.save_alert(ident, item.price)
        return False

    async def _notify(self, item: Listing, analysis: Analysis | None = None, deal: bool | str = False) -> None:
        if self._is_repeat(item, deal):
            self.skipped_dupes += 1
            log.info("повтор, не шлю: %s %s", item.key, deal)
            return
        hit_id = self.storage.add_hit(
            {
                "deal": deal,
                "item_id": item.item_id,
                "currency": item.currency,
                "analysis": analysis.to_dict() if analysis else None,
                "market": item.market,
                "collection": item.collection,
                "number": item.number,
                "price": item.price,
                "price_text": fmt_price(item),
                "model": item.model,
                "backdrop": item.backdrop,
                "symbol": item.symbol,
                "url": item.url,
                "nft_link": item.nft_link,
                "image": item.image,
            }
        )
        text, keyboard = format_listing(item, analysis, hit_id, deal)
        if caption_len(text) > 1024:  # подпись к фото ограничена 1024 видимыми символами
            item_image = None
        else:
            item_image = item.image
        for chat_id in OWNER_IDS:
            try:
                await self._send(chat_id, text, keyboard, item_image)
            except Exception as e:  # например, владелец ещё не нажал /start у бота
                log.warning("не удалось отправить %s: %s", chat_id, e)
        self.sent_total += 1
        self.deals_total += bool(deal)

    async def _send(self, chat_id: int, text: str, keyboard, image: str | None) -> None:
        if image:
            try:
                await self.bot.send_photo(chat_id, image, caption=text, reply_markup=keyboard)
                return
            except Exception as e:  # картинка не загрузилась — шлём текстом
                log.debug("send_photo: %s", e)
        await self.bot.send_message(chat_id, text, reply_markup=keyboard, disable_web_page_preview=True)

    async def close(self) -> None:
        await self.analyzer.close()
        for s in self.sources:
            await s.close()
