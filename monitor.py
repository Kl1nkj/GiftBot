import asyncio
import html
import logging
import re
import time

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from analytics import Analysis, Analyzer, format_analysis, grade_line
from config import OWNER_IDS
from models import MARKETS, Listing, norm
from sources.base import Source
from sources.fragment import FragmentSource
from sources.getgems import GetGemsSource
from sources.marketapp import MarketAppSource
from sources.mrkt import MrktSource
from sources.portals import PortalsSource
from sources.telegram_market import TelegramMarketSource
from sources.tonnel import TonnelSource
from storage import GiftFilter, Storage

log = logging.getLogger("monitor")


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
        lines.append(format_analysis(item, analysis))
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
        rows.append([InlineKeyboardButton(text="🔄 Пересчитать", callback_data=f"ra:{hit_id}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


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
        last_prune = 0.0
        while True:
            now = time.time()
            if now - last_prune > 3600:
                self.storage.prune_seen()
                self.storage.prune_price_obs()
                last_prune = now
            due = [s for s in self.sources if s.available and s.next_run <= now]
            if due and not self.paused:
                await asyncio.gather(*(self._poll(s) for s in due))
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
            items = await source.fetch(wanted)
            source.mark_ok(len(items))
        except Exception as e:  # площадка недоступна или поменяла API — пробуем снова позже
            source.last_error = f"{type(e).__name__}: {e}"[:300]
            source.next_run = time.time() + max(source.interval * 3, 60)
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
            if item.extra.get("seed"):
                continue
            candidates = [f for f in filters if f.matches_basic(item)]
            if not candidates and not deals["enabled"]:
                continue
            if item.needs_details and not candidates and not await self._worth_details(item):
                continue
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
        self.storage.mark_seen(unseen)

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

    async def _notify(self, item: Listing, analysis: Analysis | None = None, deal: bool | str = False) -> None:
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
