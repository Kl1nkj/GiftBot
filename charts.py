"""Экран «вся модель»: все лоты той же модели на площадках, реальные сделки и график цен."""
import html
import io
import json
import time
from datetime import datetime
from urllib.parse import urlencode

import matplotlib

matplotlib.use("Agg")  # без окна, только картинка
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup  # noqa: E402

from analytics import Offer, _ton  # noqa: E402
from models import MARKETS, slugify  # noqa: E402
from sales import Sale, SalesSummary  # noqa: E402

COLORS = {"portals": "#3b82f6", "tonnel": "#a855f7", "fragment": "#22c55e", "mrkt": "#f59e0b", "telegram": "#06b6d4",
          "marketapp": "#ec4899", "getgems": "#0ea5e9", "major": "#eab308"}
MIN_POINTS = 3


def fragment_model_url(collection: str, model: str) -> str:
    """Fragment открывает такую ссылку сразу с фильтром по модели и сортировкой от дешёвых."""
    query = urlencode({"attr[Model]": json.dumps([model]), "sort": "price_asc", "filter": "sale"})
    return f"https://fragment.com/gifts/{slugify(collection)}?{query}"


def model_chart(
    title: str, series: list[tuple[int, str, float]], offers: list[Offer], price: float | None,
    sales: list[Sale] | None = None,
) -> bytes | None:
    """Треугольники — реальные сделки (за сколько купили); кружки — лоты, которые продаются сейчас,
    по дате выставления (слева — висят давно, не покупают); точки — цены новых лотов, которые бот видел раньше;
    линии — флор сейчас и этот лот. offers — без самого лота."""
    now = time.time()
    sales = sales or []
    current = [(now - o.age_days * 86400 if o.age_days is not None else now, o.market, o.price) for o in offers]
    if len(series) + len(current) + len(sales) < MIN_POINTS:
        return None
    fig, ax = plt.subplots(figsize=(8, 4.2), dpi=110)
    fig.patch.set_facecolor("#17212b")
    ax.set_facecolor("#17212b")
    for market in sorted({m for _, m, _ in series}):
        pts = [(ts, p) for ts, m, p in series if m == market]
        ax.scatter(
            [datetime.fromtimestamp(ts) for ts, _ in pts], [p for _, p in pts], s=16, alpha=0.6,
            color=COLORS.get(market, "#9ca3af"), label=f"{MARKETS.get(market, market)} — выставляли",
        )
    for market in sorted({m for _, m, _ in current}):
        pts = [(ts, p) for ts, m, p in current if m == market]
        ax.scatter(
            [datetime.fromtimestamp(ts) for ts, _ in pts], [p for _, p in pts], s=70, facecolors="none",
            linewidths=1.6, edgecolors=COLORS.get(market, "#9ca3af"), label=f"{MARKETS.get(market, market)} — в продаже",
        )
    for market in sorted({s.market for s in sales}):
        pts = [s for s in sales if s.market == market]
        ax.scatter(
            [datetime.fromtimestamp(s.ts) for s in pts], [s.price for s in pts], s=60, marker="^",
            color=COLORS.get(market, "#9ca3af"), edgecolors="white", linewidths=0.6,
            label=f"{MARKETS.get(market, market.capitalize())} — купили",
        )
    first = min([ts for ts, _, _ in series] + [ts for ts, _, _ in current] + [s.ts for s in sales] + [now - 86400])
    pad = max(3600, (now - first) * 0.04)
    ax.set_xlim(datetime.fromtimestamp(first - pad), datetime.fromtimestamp(now + pad))
    if offers:
        ax.axhline(offers[0].price, color="#f87171", lw=1.4, ls="--", label=f"флор сейчас {_ton(offers[0].price)}")
    if price:
        ax.axhline(price, color="#facc15", lw=1.4, label=f"этот лот {_ton(price)}")
    ax.set_title(title, color="white", fontsize=12)
    ax.set_ylabel("TON", color="#cbd5e1")
    ax.tick_params(colors="#cbd5e1", labelsize=8)
    locator = mdates.AutoDateLocator(maxticks=8)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    for side in ax.spines.values():
        side.set_color("#334155")
    ax.grid(color="#334155", lw=0.5, alpha=0.6)
    ax.legend(facecolor="#1e293b", edgecolor="#334155", labelcolor="white", fontsize=8, loc="upper left")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()


def format_model_overview(
    collection: str, model: str, offers: list[Offer], failed: list[str],
    series: list[tuple[int, str, float]], coll_url: str | None, self_id: str | None = None,
    sales: SalesSummary | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    lines = [f"👀 <b>{html.escape(collection)} · {html.escape(model)}</b> — все такие в продаже"]
    if not offers:
        lines.append("Сейчас других лотов этой модели нет.")
    by_market: dict[str, list[Offer]] = {}
    for o in offers:
        by_market.setdefault(o.market, []).append(o)
    for market, items in by_market.items():
        links = []
        for o in items[:8]:
            age = f" ({o.age_days:.0f} дн.)" if o.age_days is not None and o.age_days >= 1 else ""
            price = _ton(o.price) + (" ← этот" if self_id and o.item_id == self_id else "")
            links.append(f'<a href="{o.url}">{price}</a>{age}' if o.url else f"{price}{age}")
        lines.append(f"<b>{MARKETS.get(market, market)}</b>: " + " · ".join(links))
    for m in failed:
        lines.append(f"<b>{MARKETS.get(m, m)}</b>: не ответил")
    if sales and sales.sales:
        prices = sorted(sales.prices)
        lines.append("")
        lines.append(
            f"💸 <b>Реально купили за 30 дн.: {len(prices)}</b> раз, середина <b>{_ton(prices[len(prices) // 2])} TON</b>, "
            f"от {_ton(prices[0])} до {_ton(prices[-1])}. Последние:"
        )
        for s in sales.sales[:8]:
            ago = (time.time() - s.ts) / 86400
            when = "сегодня" if ago < 1 else f"{ago:.0f} дн. назад"
            extra = " · ".join(html.escape(x) for x in (s.backdrop, s.symbol) if x)
            num = f"#{s.number} " if s.number else ""
            lines.append(f"• {_ton(s.price)} TON — {MARKETS.get(s.market, s.market.capitalize())}, {when}"
                         f"{f' ({num}{extra})' if num or extra else ''}")
    elif sales is not None and not sales.missing and not sales.failed:
        lines.append("")
        lines.append("💸 За 30 дней эту модель ни разу не купили — спроса почти нет.")
    if sales is not None and (sales.missing or sales.failed):
        gaps = [f"{MARKETS.get(m, m)} — не подключено" for m in sales.missing]
        gaps += [f"{MARKETS.get(m, m)} — не ответил" for m in sales.failed]
        lines.append("<i>Сделки: " + ", ".join(gaps) + "</i>")
    if series:
        prices = sorted(p for _, _, p in series)
        days = max(1, round((time.time() - series[0][0]) / 86400))
        lines.append("")
        lines.append(
            f"📈 За {days} дн. бот видел новых лотов этой модели: {len(prices)}, "
            f"от {_ton(prices[0])} до {_ton(prices[-1])}, середина {_ton(prices[len(prices) // 2])} TON"
        )
    lines.append("")
    lines.append(
        "<i>В скобках — сколько дней лот висит: долго висящий лот = по этой цене не покупают. "
        "На графике ▲ — реальные сделки, ○ — лоты в продаже сейчас, • — цены выставления, которые видел бот.</i>"
    )
    rows = [[InlineKeyboardButton(text="🔎 Эта модель на Fragment", url=fragment_model_url(collection, model))]]
    if coll_url:
        rows.append([InlineKeyboardButton(text="📂 Коллекция на Portals", url=coll_url)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)
