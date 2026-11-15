import asyncio
import html
import logging
import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonDefault,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

import user_client
from charts import format_model_overview, model_chart
from config import BOT_TOKEN, NGROK_AUTHTOKEN, NGROK_DOMAIN, OWNER_IDS, WEBAPP_HOST, WEBAPP_PORT, WEBAPP_TUNNEL, WEBAPP_URL
from models import MARKETS
from monitor import Monitor, caption_len, format_listing, listing_from_hit
from storage import GiftFilter, Storage
from tunnel import Tunnel
from webapp import start_webapp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

storage = Storage()
router = Router()
# адрес мини-приложения: из .env или от туннеля Cloudflare (меняется при каждом запуске)
webapp_url = WEBAPP_URL
SKIP = {"-", "—", "любая", "любой", "любые", "все", "*", "нет"}

HELP = (
    "<b>Команды</b>\n"
    "/app — приложение: фильтры с картинками и лента находок\n"
    "/add — добавить фильтр\n"
    "/filters — мои фильтры (вкл/выкл, удалить)\n"
    "/deals — отчёт: самые выгодные лоты за сутки\n"
    "/hunt — охотник за выгодой (пороги прибыли)\n"
    "/status — состояние площадок\n"
    "/pause, /resume — остановить/возобновить уведомления\n"
    "/cancel — отменить ввод\n\n"
    "Фильтр = коллекции + (необязательно) модели, фоны, символы, цена в TON, макс. номер и площадки. "
    "Как только на любой площадке выставят подходящий подарок — придёт уведомление.\n\n"
    "К каждому уведомлению прикладывается анализ: за сколько эта модель сейчас продаётся, "
    "за сколько её обычно выставляют, сколько останется после комиссии при перепродаже и стоит ли брать. "
    "Охотник за выгодой сам присылает 🔥 лоты, которые можно перепродать в плюс, даже без фильтров."
)


class AddFilter(StatesGroup):
    collections = State()
    models = State()
    backdrops = State()
    symbols = State()
    price = State()
    number = State()
    markets = State()


def parse_list(text: str) -> list[str]:
    if text.strip().lower() in SKIP:
        return []
    return [p.strip() for p in text.replace("\n", ",").split(",") if p.strip()]


def parse_price(text: str) -> tuple[float | None, float | None]:
    """'10-50', '-50', '10-', '50', '-' → (мин, макс)."""
    t = text.strip().replace(",", ".").replace(" ", "").lower()
    if t in SKIP:
        return None, None
    if "-" not in t:
        return None, float(t)
    lo, hi = t.split("-", 1)
    return (float(lo) if lo else None), (float(hi) if hi else None)


def describe(f: GiftFilter) -> str:
    def lst(values):
        return html.escape(", ".join(values)) if values else "любые"

    if f.min_price is None and f.max_price is None:
        price = "любая"
    else:
        price = f"{f.min_price if f.min_price is not None else 0:g} – {f'{f.max_price:g}' if f.max_price is not None else '∞'} TON"
    parts = [
        f"{'🟢' if f.enabled else '⏸'} <b>Фильтр #{f.id}</b>",
        f"Коллекции: {lst(f.collections) if f.collections else 'любые'}",
        f"Модели: {lst(f.models)}",
        f"Фоны: {lst(f.backdrops)}",
        f"Символы: {lst(f.symbols)}",
        f"Цена: {price}",
    ]
    if f.max_number:
        parts.append(f"Номер: до #{f.max_number}")
    parts.append("Площадки: " + (", ".join(MARKETS[m] for m in f.markets) if f.markets else "все"))
    return "\n".join(parts)


def markets_keyboard(selected: list[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=("✅ " if key in selected else "▫️ ") + title, callback_data=f"mk:{key}")]
        for key, title in MARKETS.items()
    ]
    rows.append([InlineKeyboardButton(text="Готово (ничего не выбрано = все)", callback_data="mk:done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def filter_keyboard(f: GiftFilter) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="⏸ Выключить" if f.enabled else "▶️ Включить", callback_data=f"ft:{f.id}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"fd:{f.id}"),
        ]]
    )


# --- доступ только владельцу ---
@router.message(~F.from_user.id.in_(OWNER_IDS))
async def not_owner(message: Message):
    if not OWNER_IDS:
        await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>\nВпишите его в .env как OWNER_ID и перезапустите бота.")


@router.callback_query(~F.from_user.id.in_(OWNER_IDS))
async def not_owner_cb(cb: CallbackQuery):
    await cb.answer()


def app_keyboard() -> InlineKeyboardMarkup | None:
    if not webapp_url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🎁 Открыть приложение", web_app=WebAppInfo(url=webapp_url))]]
    )


@router.message(CommandStart())
@router.message(Command("help"))
async def start(message: Message):
    await message.answer(
        "Привет! Я слежу за подарками на площадках и пишу, когда выставляют подходящие.\n\n" + HELP,
        reply_markup=app_keyboard(),
    )


@router.message(Command("app"))
async def open_app(message: Message):
    if not webapp_url:
        await message.answer("Мини-приложение недоступно: нет WEBAPP_URL и не удалось запустить туннель (см. README).")
        return
    await message.answer("Фильтры и лента находок:", reply_markup=app_keyboard())


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.")


@router.message(Command("add"))
async def add_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(AddFilter.collections)
    await message.answer(
        "<b>1/7. Коллекции</b>\nНазвания через запятую, как на площадках, например:\n"
        "<code>Plush Pepe, Durov's Cap</code>\n\nИли <code>-</code> — любая коллекция."
    )


@router.message(AddFilter.collections)
async def add_collections(message: Message, state: FSMContext):
    await state.update_data(collections=parse_list(message.text or ""))
    await state.set_state(AddFilter.models)
    await message.answer("<b>2/7. Модели</b> через запятую или <code>-</code> — любые.")


@router.message(AddFilter.models)
async def add_models(message: Message, state: FSMContext):
    await state.update_data(models=parse_list(message.text or ""))
    await state.set_state(AddFilter.backdrops)
    await message.answer("<b>3/7. Фоны</b> через запятую или <code>-</code> — любые.")


@router.message(AddFilter.backdrops)
async def add_backdrops(message: Message, state: FSMContext):
    await state.update_data(backdrops=parse_list(message.text or ""))
    await state.set_state(AddFilter.symbols)
    await message.answer("<b>4/7. Символы (узоры)</b> через запятую или <code>-</code> — любые.")


@router.message(AddFilter.symbols)
async def add_symbols(message: Message, state: FSMContext):
    await state.update_data(symbols=parse_list(message.text or ""))
    await state.set_state(AddFilter.price)
    await message.answer(
        "<b>5/7. Цена в TON</b>\n<code>10-50</code> — от 10 до 50\n<code>50</code> — до 50\n"
        "<code>100-</code> — от 100\n<code>-</code> — любая"
    )


@router.message(AddFilter.price)
async def add_price(message: Message, state: FSMContext):
    try:
        lo, hi = parse_price(message.text or "")
    except ValueError:
        await message.answer("Не понял цену. Пример: <code>10-50</code> или <code>-</code>")
        return
    await state.update_data(min_price=lo, max_price=hi)
    await state.set_state(AddFilter.number)
    await message.answer("<b>6/7. Максимальный номер подарка</b> (например <code>1000</code>) или <code>-</code> — любой.")


@router.message(AddFilter.number)
async def add_number(message: Message, state: FSMContext):
    text = (message.text or "").strip().lstrip("#")
    if text.lower() in SKIP:
        max_number = None
    elif text.isdigit():
        max_number = int(text)
    else:
        await message.answer("Введите число или <code>-</code>")
        return
    await state.update_data(max_number=max_number, markets=[])
    await state.set_state(AddFilter.markets)
    await message.answer("<b>7/7. Площадки</b>", reply_markup=markets_keyboard([]))


@router.callback_query(AddFilter.markets, F.data.startswith("mk:"))
async def add_markets(cb: CallbackQuery, state: FSMContext):
    key = cb.data[3:]
    data = await state.get_data()
    markets = list(data.get("markets", []))
    if key != "done":
        markets.remove(key) if key in markets else markets.append(key)
        await state.update_data(markets=markets)
        await cb.message.edit_reply_markup(reply_markup=markets_keyboard(markets))
        await cb.answer()
        return
    f = GiftFilter(
        collections=data["collections"],
        models=data["models"],
        backdrops=data["backdrops"],
        symbols=data["symbols"],
        min_price=data["min_price"],
        max_price=data["max_price"],
        max_number=data["max_number"],
        markets=markets,
    )
    f.id = storage.add_filter(f)
    await state.clear()
    await cb.message.edit_text("✅ Фильтр сохранён\n\n" + describe(f))
    await cb.answer()


@router.message(Command("filters"))
async def list_filters(message: Message):
    filters = storage.list_filters()
    if not filters:
        await message.answer("Фильтров пока нет. Добавьте: /add")
        return
    for f in filters:
        await message.answer(describe(f), reply_markup=filter_keyboard(f))


@router.callback_query(F.data.startswith("ft:"))
async def toggle_filter(cb: CallbackQuery):
    fid = int(cb.data[3:])
    f = next((x for x in storage.list_filters() if x.id == fid), None)
    if f:
        f.enabled = not f.enabled
        storage.update_filter(f)
        await cb.message.edit_text(describe(f), reply_markup=filter_keyboard(f))
    await cb.answer()


@router.callback_query(F.data.startswith("fd:"))
async def delete_filter(cb: CallbackQuery):
    storage.delete_filter(int(cb.data[3:]))
    await cb.message.edit_text("🗑 Фильтр удалён")
    await cb.answer()


@router.message(Command("pause"))
async def pause(message: Message):
    storage.set_setting("paused", "1")
    await message.answer("⏸ Уведомления на паузе. /resume — возобновить.")


@router.message(Command("resume"))
async def resume(message: Message):
    storage.set_setting("paused", "0")
    await message.answer("▶️ Уведомления включены.")


@router.message(Command("status"))
async def status(message: Message, monitor: Monitor):
    lines = ["<b>Площадки</b>"]
    for s in monitor.sources:
        title = MARKETS[s.name]
        if not s.available:
            lines.append(f"⚪️ {title} — выключена: {s.unavailable_reason}")
        elif s.last_error:
            lines.append(f"🔴 {title} — ошибка: <code>{html.escape(s.last_error[:150])}</code>")
        elif s.last_ok:
            lines.append(f"🟢 {title} — {s.last_count} лотов, {int(time.time() - s.last_ok)} с назад")
        else:
            lines.append(f"🟡 {title} — ещё не опрашивалась (нужен фильтр для этой площадки)")
    lines.append("")
    lines.append(f"Фильтров: {len(storage.list_filters())} · уведомлений с запуска: {monitor.sent_total}")
    deals = monitor.analyzer.deal_settings()
    lines.append(
        f"🔥 Охотник за выгодой: {'вкл' if deals['enabled'] else 'выкл'} (от {deals['min_roi']:g}%) · "
        f"найдено с запуска: {monitor.deals_total}"
    )
    src = monitor.analyzer.sales.sources
    names = {"portals": "Portals", "tonnel": "Tonnel", "marketapp": "блокчейн (MarketApp)", "telegram": "оценка Telegram"}
    on = [names[k] for k, v in src.items() if v]
    off = [names[k] for k, v in src.items() if not v]
    lines.append("💸 Реальные продажи: " + (", ".join(on) if on else "не подключены")
                 + (f" · нет: {', '.join(off)}" if off else ""))
    if monitor.paused:
        lines.append("⏸ Сейчас на паузе")
    await message.answer("\n".join(lines))


def _short_ton(v) -> str:
    return "—" if v is None else f"{v:,.2f}".rstrip("0").rstrip(".").replace(",", " ")


@router.message(Command("deals"))
async def deals_report(message: Message):
    since = int(time.time()) - 86400
    hits = [h for h in storage.list_hits(1000, since) if (h.get("analysis") or {}).get("ok")]
    hits.sort(key=lambda h: h["analysis"]["roi"], reverse=True)
    if not hits:
        await message.answer(
            "За сутки ещё нет проанализированных лотов. Охотник присылает 🔥 выгодные лоты сам, "
            "а здесь будет отчёт по всем находкам. Пороги — /hunt"
        )
        return
    icons = {"buy": "🟢", "maybe": "🟡", "skip": "🔴"}
    lines = ["📈 <b>Самые выгодные лоты за 24 часа</b>", ""]
    for h in hits[:15]:
        a = h["analysis"]
        title = html.escape(h["collection"]) + (f" #{h['number']}" if h.get("number") is not None else "")
        model = f" · {html.escape(h['model'])}" if h.get("model") else ""
        link = h.get("nft_link") or h.get("url")
        name = f'<a href="{link}">{title}</a>' if link else title
        sign = "+" if a["profit"] >= 0 else "−"
        lines.append(
            f"{icons.get(a['verdict'], '⚪')} {name}{model}\n"
            f"    {_short_ton(h.get('price'))} TON на {MARKETS.get(h['market'], h['market'])} · "
            f"рынок {_short_ton(a.get('model_floor'))} · "
            f"<b>{sign}{_short_ton(abs(a['profit']))} TON ({a['roi'] * 100:+.0f}%)</b>"
        )
    lines.append("\nЦены на момент находки — перед покупкой жми «🔄 Пересчитать» в уведомлении.")
    await message.answer("\n".join(lines), disable_web_page_preview=True, reply_markup=app_keyboard())


def hunt_text(settings: dict) -> str:
    max_price = f"{_short_ton(settings['max_price'])} TON" if settings["max_price"] else "любая"
    return (
        f"🔥 <b>Охотник за выгодой: {'включён' if settings['enabled'] else 'выключен'}</b>\n\n"
        "Проверяет каждый новый лот на всех площадках (без учёта фильтров) и присылает те, "
        "что можно перепродать в плюс после комиссии.\n\n"
        f"Мин. доходность: <b>{settings['min_roi']:g}%</b>\n"
        f"Мин. прибыль: <b>{settings['min_profit']:g} TON</b>\n"
        f"Дешевле рынка минимум на: <b>{settings['min_discount']:g}%</b>\n"
        f"Макс. цена лота: <b>{max_price}</b>\n"
        f"Только ликвидные коллекции: <b>{'да' if settings['only_liquid'] else 'нет'}</b>\n\n"
        "Изменить числа: <code>/hunt 15 1 100 7</code> — доходность %, прибыль TON, "
        "макс. цена (0 = любая), скидка к рынку %."
    )


def hunt_keyboard(settings: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏸ Выключить" if settings["enabled"] else "▶️ Включить", callback_data="hu:on")],
        [InlineKeyboardButton(text=f"{v}%", callback_data=f"hu:roi:{v}") for v in (10, 15, 20, 30, 50)],
        [InlineKeyboardButton(
            text=("✅ " if settings["only_liquid"] else "▫️ ") + "Только ликвидные", callback_data="hu:liq"
        )],
        [InlineKeyboardButton(
            text=("✅ " if settings["numbers"] else "▫️ ") + "💎 Красивые номера по цене обычных",
            callback_data="hu:num",
        )],
    ])


@router.message(Command("hunt"))
async def hunt(message: Message, monitor: Monitor):
    analyzer = monitor.analyzer
    args = (message.text or "").split()[1:]
    if args:
        try:
            nums = [float(a.replace(",", ".")) for a in args[:4]]
        except ValueError:
            await message.answer("Формат: <code>/hunt 15 1 100 7</code>")
            return
        data = {"min_roi": nums[0]}
        if len(nums) > 1:
            data["min_profit"] = nums[1]
        if len(nums) > 2:
            data["max_price"] = nums[2] or None
        if len(nums) > 3:
            data["min_discount"] = nums[3]
        analyzer.save_deal_settings(data)
    settings = analyzer.deal_settings()
    await message.answer(hunt_text(settings), reply_markup=hunt_keyboard(settings))


@router.callback_query(F.data.startswith("hu:"))
async def hunt_cb(cb: CallbackQuery, monitor: Monitor):
    analyzer = monitor.analyzer
    settings = analyzer.deal_settings()
    parts = cb.data.split(":")
    if parts[1] == "on":
        settings = analyzer.save_deal_settings({"enabled": not settings["enabled"]})
    elif parts[1] == "roi":
        settings = analyzer.save_deal_settings({"min_roi": float(parts[2])})
    elif parts[1] == "liq":
        settings = analyzer.save_deal_settings({"only_liquid": not settings["only_liquid"]})
    elif parts[1] == "num":
        settings = analyzer.save_deal_settings({"numbers": not settings["numbers"]})
    try:
        await cb.message.edit_text(hunt_text(settings), reply_markup=hunt_keyboard(settings))
    except Exception as e:  # «message is not modified»
        logging.debug("hunt edit: %s", e)
    await cb.answer("Сохранено")


@router.callback_query(F.data.startswith("ra:"))
async def reanalyze(cb: CallbackQuery, monitor: Monitor):
    hit = storage.get_hit(int(cb.data[3:]))
    if not hit:
        await cb.answer("Лот уже удалён из истории", show_alert=True)
        return
    await cb.answer("Считаю по свежим ценам…")
    item = listing_from_hit(hit)
    try:
        analysis = await asyncio.wait_for(monitor.analyzer.analyze(item, fresh=True, check_listed=True), 30)
    except Exception as e:
        await cb.message.answer(f"Не удалось пересчитать: {html.escape(str(e) or type(e).__name__)}")
        return
    hit["analysis"] = analysis.to_dict()
    storage.update_hit(hit["id"], hit)
    text, keyboard = format_listing(item, analysis, hit["id"], hit.get("deal", False))
    text += f"\n\n<i>Пересчитано {time.strftime('%H:%M:%S')}</i>"
    try:
        if cb.message.photo and caption_len(text) <= 1024:
            await cb.message.edit_caption(caption=text, reply_markup=keyboard)
        elif cb.message.photo:
            await cb.message.answer(text, reply_markup=keyboard, disable_web_page_preview=True)
        else:
            await cb.message.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True)
    except Exception as e:  # «message is not modified» и т.п.
        logging.debug("reanalyze edit: %s", e)


@router.callback_query(F.data.startswith("md:"))
async def model_overview(cb: CallbackQuery, monitor: Monitor):
    hit = storage.get_hit(int(cb.data[3:]))
    if not hit or not hit.get("model"):
        await cb.answer("Лот уже удалён из истории", show_alert=True)
        return
    await cb.answer("Собираю все лоты этой модели…")
    an = monitor.analyzer
    try:
        (offers, failed), sales = await asyncio.wait_for(asyncio.gather(
            an.model_overview(hit["collection"], hit["model"]),
            an._model_sales(hit["collection"], hit["model"], fresh=False),
        ), 40)
    except Exception as e:
        await cb.message.answer(f"Не удалось загрузить: {html.escape(str(e) or type(e).__name__)}")
        return
    series = an.price_series(hit["collection"], hit["model"])
    coll_url = (hit.get("analysis") or {}).get("coll_url")
    self_id = str(hit.get("item_id") or "")
    text, keyboard = format_model_overview(
        hit["collection"], hit["model"], offers, failed, series, coll_url, self_id, sales
    )
    others = [o for o in offers if not (o.market == hit["market"] and o.item_id == self_id)]
    chart = await asyncio.to_thread(
        model_chart, f"{hit['collection']} · {hit['model']}", series, others, hit.get("price"),
        sales.sales if sales else None,
    )
    if chart and caption_len(text) <= 1024:
        await cb.message.answer_photo(BufferedInputFile(chart, "model.png"), caption=text, reply_markup=keyboard)
        return
    if chart:
        await cb.message.answer_photo(BufferedInputFile(chart, "model.png"))
    await cb.message.answer(text, reply_markup=keyboard, disable_web_page_preview=True)


async def set_menu_button(bot: Bot) -> None:
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Подарки", web_app=WebAppInfo(url=webapp_url)))


async def keep_tunnel(bot: Bot, tunnel: Tunnel) -> None:
    """Держит туннель для мини-приложения в фоне: бот работает и без него,
    а при обрыве туннель поднимается заново с новым адресом."""
    global webapp_url

    async def on_change(url: str) -> None:
        global webapp_url
        webapp_url = url
        await set_menu_button(bot)

    tunnel.on_change = on_change
    while True:
        try:
            await on_change(await tunnel.start())
            await tunnel.proc.wait()
            logging.warning("Туннель оборвался, перезапускаю")
            delay = 10
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.warning("Туннель не запустился (%s) — повтор через 2 мин", e)
            delay = 120
        webapp_url = ""
        await tunnel.stop()
        await asyncio.sleep(delay)


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Заполните BOT_TOKEN в .env")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    monitor = Monitor(bot, storage)
    dp["monitor"] = monitor

    await bot.set_my_commands([
        BotCommand(command="app", description="Открыть приложение"),
        BotCommand(command="add", description="Добавить фильтр"),
        BotCommand(command="filters", description="Мои фильтры"),
        BotCommand(command="deals", description="Отчёт: выгодные лоты за сутки"),
        BotCommand(command="hunt", description="Охотник за выгодой"),
        BotCommand(command="status", description="Состояние площадок"),
        BotCommand(command="pause", description="Пауза"),
        BotCommand(command="resume", description="Возобновить"),
    ])
    web_runner = await start_webapp(storage, monitor, WEBAPP_HOST, WEBAPP_PORT)
    tunnel = None
    tunnel_task = None
    if webapp_url:
        await set_menu_button(bot)
    else:
        # убираем кнопку со старым адресом прошлого туннеля, пока не поднимется новый
        await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
    if not webapp_url and WEBAPP_TUNNEL:
        tunnel = Tunnel(WEBAPP_PORT, ngrok_token=NGROK_AUTHTOKEN, ngrok_domain=NGROK_DOMAIN)
    if tunnel and tunnel.available():
        tunnel_task = asyncio.create_task(keep_tunnel(bot, tunnel))
    monitor_task = asyncio.create_task(monitor.run())
    try:
        await dp.start_polling(bot)
    finally:
        monitor_task.cancel()
        if tunnel_task:
            tunnel_task.cancel()
        await web_runner.cleanup()
        if tunnel:
            await tunnel.stop()
        await monitor.close()
        await user_client.disconnect()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
