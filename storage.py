import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field

from config import DB_PATH
from models import Listing, norm


@dataclass
class GiftFilter:
    id: int | None = None
    collections: list[str] = field(default_factory=list)  # пусто = любая коллекция
    models: list[str] = field(default_factory=list)
    backdrops: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    min_price: float | None = None
    max_price: float | None = None
    max_number: int | None = None
    markets: list[str] = field(default_factory=list)  # пусто = все площадки
    enabled: bool = True

    def needs_attributes(self) -> bool:
        return bool(self.models or self.backdrops or self.symbols)

    def matches_basic(self, item: Listing) -> bool:
        """Проверка без атрибутов (коллекция, цена, номер, площадка)."""
        if not self.enabled:
            return False
        if self.markets and item.market not in self.markets:
            return False
        if self.collections and norm(item.collection) not in {norm(c) for c in self.collections}:
            return False
        if item.price is not None:
            if self.min_price is not None and item.price < self.min_price:
                return False
            if self.max_price is not None and item.price > self.max_price:
                return False
        elif self.min_price is not None or self.max_price is not None:
            return False
        if self.max_number is not None and (item.number is None or item.number > self.max_number):
            return False
        return True

    def matches(self, item: Listing) -> bool:
        if not self.matches_basic(item):
            return False
        for wanted, actual in (
            (self.models, item.model),
            (self.backdrops, item.backdrop),
            (self.symbols, item.symbol),
        ):
            if wanted and norm(actual) not in {norm(w) for w in wanted}:
                return False
        return True


class Storage:
    def __init__(self, path: str = DB_PATH):
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")  # чтение не ждёт записи, база не бьётся при обрыве
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS alerts (ident TEXT PRIMARY KEY, ts INTEGER NOT NULL, price REAL);
            CREATE TABLE IF NOT EXISTS positions (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS filters (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, ts INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS hits (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS price_obs (ts INTEGER NOT NULL, coll TEXT NOT NULL, model TEXT NOT NULL,
                                                  market TEXT NOT NULL, price REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS price_obs_model ON price_obs (coll, model, ts);
            """
        )
        self.db.commit()

    # --- фильтры ---
    def list_filters(self) -> list[GiftFilter]:
        rows = self.db.execute("SELECT id, data FROM filters ORDER BY id").fetchall()
        return [GiftFilter(id=row[0], **{k: v for k, v in json.loads(row[1]).items() if k != "id"}) for row in rows]

    def add_filter(self, f: GiftFilter) -> int:
        data = asdict(f)
        data.pop("id")
        cur = self.db.execute("INSERT INTO filters (data) VALUES (?)", (json.dumps(data, ensure_ascii=False),))
        self.db.commit()
        return cur.lastrowid

    def update_filter(self, f: GiftFilter) -> None:
        data = asdict(f)
        data.pop("id")
        self.db.execute("UPDATE filters SET data = ? WHERE id = ?", (json.dumps(data, ensure_ascii=False), f.id))
        self.db.commit()

    def delete_filter(self, filter_id: int) -> None:
        self.db.execute("DELETE FROM filters WHERE id = ?", (filter_id,))
        self.db.commit()

    # --- просмотренные лоты ---
    def filter_unseen(self, keys: list[str]) -> set[str]:
        if not keys:
            return set()
        placeholders = ",".join("?" * len(keys))
        seen = {r[0] for r in self.db.execute(f"SELECT key FROM seen WHERE key IN ({placeholders})", keys)}
        return set(keys) - seen

    def mark_seen(self, keys) -> None:
        """Время обновляется при каждом появлении лота: чистка удаляет только пропавшие лоты,
        и висящий дольше двух недель лот не придёт повторно как новый."""
        now = int(time.time())
        self.db.executemany(
            "INSERT INTO seen (key, ts) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET ts = excluded.ts",
            [(k, now) for k in keys],
        )
        self.db.commit()

    def prune_seen(self, max_age_days: int = 14) -> None:
        self.db.execute("DELETE FROM seen WHERE ts < ?", (int(time.time()) - max_age_days * 86400,))
        self.db.commit()

    # --- найденные лоты (лента в мини-приложении) ---
    def add_hit(self, data: dict) -> int:
        cur = self.db.execute("INSERT INTO hits (ts, data) VALUES (?, ?)", (int(time.time()), json.dumps(data, ensure_ascii=False)))
        self.db.execute("DELETE FROM hits WHERE id <= (SELECT MAX(id) - 5000 FROM hits)")
        self.db.commit()
        return cur.lastrowid

    def get_hit(self, hit_id: int) -> dict | None:
        row = self.db.execute("SELECT id, ts, data FROM hits WHERE id = ?", (hit_id,)).fetchone()
        return {**json.loads(row[2]), "id": row[0], "ts": row[1]} if row else None

    def update_hit(self, hit_id: int, data: dict) -> None:
        clean = {k: v for k, v in data.items() if k not in ("id", "ts")}
        self.db.execute("UPDATE hits SET data = ? WHERE id = ?", (json.dumps(clean, ensure_ascii=False), hit_id))
        self.db.commit()

    def list_hits(self, limit: int = 100, since: int = 0) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, ts, data FROM hits WHERE ts >= ? ORDER BY id DESC LIMIT ?", (since, limit)
        ).fetchall()
        return [{**json.loads(data), "id": hit_id, "ts": ts} for hit_id, ts, data in rows]

    # --- история цен (для «обычной» цены модели) ---
    def add_price_obs(self, rows: list[tuple[str, str, str, float]]) -> None:
        if not rows:
            return
        now = int(time.time())
        self.db.executemany(
            "INSERT INTO price_obs (ts, coll, model, market, price) VALUES (?, ?, ?, ?, ?)",
            [(now, *r) for r in rows],
        )
        self.db.commit()

    def price_obs(self, coll: str, model: str, max_age: int) -> list[float]:
        rows = self.db.execute(
            "SELECT price FROM price_obs WHERE coll = ? AND model = ? AND ts >= ?",
            (coll, model, int(time.time()) - max_age),
        ).fetchall()
        return [r[0] for r in rows]

    def price_series(self, coll: str, model: str, max_age: int) -> list[tuple[int, str, float]]:
        return self.db.execute(
            "SELECT ts, market, price FROM price_obs WHERE coll = ? AND model = ? AND ts >= ? ORDER BY ts",
            (coll, model, int(time.time()) - max_age),
        ).fetchall()

    def prune_price_obs(self, max_age_days: int = 30) -> None:
        self.db.execute("DELETE FROM price_obs WHERE ts < ?", (int(time.time()) - max_age_days * 86400,))
        self.db.commit()

    # --- антиспам: последний алерт по подарку ---
    def last_alert(self, ident: str, max_age: int) -> float | None:
        row = self.db.execute(
            "SELECT price FROM alerts WHERE ident = ? AND ts >= ?", (ident, int(time.time()) - max_age)
        ).fetchone()
        return row[0] if row else None

    def save_alert(self, ident: str, price: float | None) -> None:
        self.db.execute("INSERT OR REPLACE INTO alerts (ident, ts, price) VALUES (?, ?, ?)",
                        (ident, int(time.time()), price))
        self.db.execute("DELETE FROM alerts WHERE ts < ?", (int(time.time()) - 7 * 86400,))
        self.db.commit()

    # --- журнал сделок (купленные подарки) ---
    def add_position(self, data: dict) -> int:
        cur = self.db.execute("INSERT INTO positions (data) VALUES (?)", (json.dumps(data, ensure_ascii=False),))
        self.db.commit()
        return cur.lastrowid

    def get_position(self, pos_id: int) -> dict | None:
        row = self.db.execute("SELECT id, data FROM positions WHERE id = ?", (pos_id,)).fetchone()
        return {**json.loads(row[1]), "id": row[0]} if row else None

    def update_position(self, pos_id: int, data: dict) -> None:
        clean = {k: v for k, v in data.items() if k != "id"}
        self.db.execute("UPDATE positions SET data = ? WHERE id = ?", (json.dumps(clean, ensure_ascii=False), pos_id))
        self.db.commit()

    def delete_position(self, pos_id: int) -> None:
        self.db.execute("DELETE FROM positions WHERE id = ?", (pos_id,))
        self.db.commit()

    def list_positions(self) -> list[dict]:
        rows = self.db.execute("SELECT id, data FROM positions ORDER BY id DESC").fetchall()
        return [{**json.loads(d), "id": i} for i, d in rows]

    # --- настройки ---
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        self.db.commit()
