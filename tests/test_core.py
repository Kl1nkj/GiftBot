"""Тесты чистых функций: без сети и без настоящей базы. Запуск: python -m pytest  (или python tests/run.py)."""
import asyncio
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "1")

from analytics import Analysis, Analyzer, clean_sales, format_analysis, format_analysis_short, weighted_median  # noqa: E402
from models import Listing, number_pattern  # noqa: E402
from sales import Sale, SalesSummary  # noqa: E402
from storage import GiftFilter, Storage  # noqa: E402

NOW = time.time()
DAY = 86400


def item(**kw) -> Listing:
    base = dict(market="portals", item_id="1", collection="Mini Oscar", number=4242, price=10.0,
                model="Ballerino", backdrop="Black", symbol="Star")
    base.update(kw)
    return Listing(**base)


# ---------- номера ----------
def test_number_patterns():
    assert number_pattern(7)[0] == 3
    assert number_pattern(555)[0] == 3
    assert number_pattern(183183)[0] == 2
    assert number_pattern(50000)[0] == 2
    assert number_pattern(1234)[0] == 2
    assert number_pattern(12321)[0] == 1
    assert number_pattern(48213)[0] == 0
    # «до #1000» в маленьком тираже — слабый признак
    assert number_pattern(777, 3000)[0] == 3  # одинаковые цифры — всегда топ
    assert number_pattern(812, 3000)[0] == 1
    assert number_pattern(812, 200000)[0] == 2


# ---------- сделки ----------
def test_clean_sales_drops_wash_and_outliers():
    sales = [Sale("portals", int(NOW - i * DAY), 10.0 + i * 0.1, number=100 + i) for i in range(8)]
    sales.append(Sale("portals", int(NOW - 0.5 * DAY), 11.0, number=100))  # тот же номер через полдня — флип
    sales.append(Sale("tonnel", int(NOW - 2 * DAY), 95.0, number=999))  # выброс
    clean = clean_sales(sales)
    assert all(s.price < 50 for s in clean)
    assert sum(1 for s in clean if s.number == 100) == 1


def test_weighted_median_prefers_recent():
    pairs = [(10, 1.0), (10, 1.0), (20, 0.1), (20, 0.1), (20, 0.1)]
    assert weighted_median(pairs) == 10


def test_apply_sales_model_liquidity_and_combo():
    a = Analysis()
    sales = [Sale("portals", int(NOW - i * 3 * DAY), 10.0, number=i, backdrop="Black" if i < 3 else "Red")
             for i in range(9)]
    Analyzer._apply_sales(item(), a, SalesSummary(sales, [], ["marketapp"]))
    assert a.sales_n == 9
    assert a.liquidity_by == "model" and a.sales_7d == 3
    assert a.days_to_sell is not None and 2 <= a.days_to_sell <= 3
    assert a.combo_sales_n == 3  # «тот же фон» — только за 14 дней


# ---------- оценка рынка не выше такого же лота в продаже ----------
def test_fair_capped_by_listed_combo():
    a = Analysis(sales_n=5, sales_median=20.0, combo_floor=14.0, combo_count=3, model_floor=12.0)
    Analyzer._fair_value(item(price=10.0), a)
    assert a.fair == 14.0
    assert "не дороже такого же" in a.fair_basis


def test_fair_not_capped_by_stale_floor():
    a = Analysis(sales_n=5, sales_median=20.0, model_floor=12.0, floor_age=30)
    Analyzer._fair_value(item(price=10.0), a)
    assert a.fair == 20.0


# ---------- форматы ----------
def test_short_alert_fits_caption():
    a = Analysis(ok=True, verdict="buy", reasons=["a" * 80] * 6, model_floor=12, floor_market="portals",
                 sales_n=5, sales_median=13, days_to_sell=2, sell_price=11.9, sell_market="portals", sell_fee=0.05,
                 net=11.3, profit=1.3, roi=0.13, fair=13, fair_basis="по 5 сделкам", fair_discount=0.2,
                 confidence="high", buy_cost=10)
    short = format_analysis_short(item(), a)
    assert len(short) < 900
    assert "📊 Подробно" in short
    assert len(format_analysis(item(), a)) > len(short)


# ---------- фильтры ----------
def test_filter_matches():
    f = GiftFilter(collections=["mini oscar"], models=["ballerino"], max_price=11, max_number=5000)
    assert f.matches(item())
    assert not f.matches(item(price=12))
    assert not f.matches(item(model="Other"))
    assert not f.matches(item(number=6000))


# ---------- база: антиспам, seen, журнал ----------
def _storage() -> Storage:
    return Storage(os.path.join(tempfile.mkdtemp(), "t.db"))


def test_seen_upsert_and_alerts_and_positions():
    s = _storage()
    s.mark_seen(["a"])
    s.db.execute("UPDATE seen SET ts = 0")
    s.mark_seen(["a"])  # лот всё ещё висит — время обновилось, чистка его не удалит
    s.prune_seen()
    assert s.filter_unseen(["a"]) == set()
    s.save_alert("x#1", 10.0)
    assert s.last_alert("x#1", 3600) == 10.0
    pid = s.add_position({"status": "open", "cost": 5})
    p = s.get_position(pid)
    p["status"] = "closed"
    s.update_position(pid, p)
    assert s.list_positions()[0]["status"] == "closed"


def test_monitor_dedupe():
    from monitor import Monitor
    m = Monitor.__new__(Monitor)
    m.storage = _storage()
    m._model_alerts = {}
    assert not m._is_repeat(item(price=10), True)
    assert m._is_repeat(item(price=9.9, market="tonnel"), True)  # тот же подарок на другой площадке, почти та же цена
    assert not m._is_repeat(item(price=9.0), True)  # подешевел больше чем на 3% — сообщаем
    for n in range(10, 13):
        m._is_repeat(item(number=n, price=10), True)
    assert m._is_repeat(item(number=99, price=10), True)  # волна одной модели: не больше 3 в час


# ---------- проверка входа мини-приложения ----------
def test_check_init_data():
    from webapp import check_init_data
    token = os.environ["BOT_TOKEN"]
    pairs = {"auth_date": str(int(time.time())), "user": json.dumps({"id": 1})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    assert check_init_data(urlencode(pairs)) == {"id": 1}
    assert check_init_data("hash=abc&auth_date=x") is None
    pairs["auth_date"] = "bad"
    assert check_init_data(urlencode(pairs)) is None


def test_caption_len():
    from monitor import caption_len
    assert caption_len("<b>a&amp;b</b>") == 3


if __name__ == "__main__":
    asyncio.run(asyncio.sleep(0))
