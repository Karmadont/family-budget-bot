"""
db.py — хранилище покупок на SQLite.

Одна строка = одна позиция чека. Сообщение из чата может дать несколько строк,
они связаны общим message_id (это же позволяет откатить последнюю запись целиком).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import aiosqlite

import config
from models import ParsedMessage

_conn: aiosqlite.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS purchases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER,
    user_id     INTEGER,
    user_name   TEXT,
    bought_at   TEXT    NOT NULL,          -- YYYY-MM-DD
    name        TEXT    NOT NULL,
    category    TEXT    NOT NULL,
    quantity    REAL,
    unit        TEXT,
    price       REAL    NOT NULL,          -- итог по позиции
    store       TEXT,
    is_food     INTEGER NOT NULL DEFAULT 0,
    perishable  INTEGER NOT NULL DEFAULT 0,
    consumed_at TEXT,                      -- NULL = ещё не съедено
    raw_text    TEXT,
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_purchases_chat_date ON purchases (chat_id, bought_at);
CREATE INDEX IF NOT EXISTS idx_purchases_chat_cat  ON purchases (chat_id, category);
CREATE INDEX IF NOT EXISTS idx_purchases_fridge    ON purchases (chat_id, is_food, consumed_at);
"""


@dataclass(slots=True)
class Purchase:
    """Строка покупки, как она лежит в базе."""

    id: int
    bought_at: str
    name: str
    category: str
    quantity: float | None
    unit: str | None
    price: float
    store: str | None
    is_food: bool
    perishable: bool
    user_name: str | None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "Purchase":
        return cls(
            id=row["id"],
            bought_at=row["bought_at"],
            name=row["name"],
            category=row["category"],
            quantity=row["quantity"],
            unit=row["unit"],
            price=row["price"],
            store=row["store"],
            is_food=bool(row["is_food"]),
            perishable=bool(row["perishable"]),
            user_name=row["user_name"],
        )


def _lower(value):
    """Регистронезависимость для кириллицы.

    Встроенный lower() в SQLite умеет только ASCII: 'Молоко' и 'молоко' для него
    разные строки. Поэтому регистр приводим питоновским str.lower().
    """
    return value.lower() if isinstance(value, str) else value


async def init() -> None:
    """Открыть соединение и создать таблицы, если их ещё нет."""
    global _conn
    _conn = await aiosqlite.connect(config.DB_PATH)
    _conn.row_factory = aiosqlite.Row
    await _conn.create_function("pylower", 1, _lower, deterministic=True)
    await _conn.executescript(SCHEMA)
    await _conn.commit()


async def close() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


def _db() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("db.init() не вызван")
    return _conn


# --- запись -----------------------------------------------------------------

async def save_parsed(
    *,
    chat_id: int,
    message_id: int | None,
    user_id: int | None,
    user_name: str | None,
    raw_text: str,
    parsed: ParsedMessage,
    default_date: str,
) -> int:
    """Сохранить все позиции разобранного сообщения. Возвращает количество строк."""
    bought_at = parsed.bought_on or default_date
    now = dt.datetime.now(config.TIMEZONE).isoformat(timespec="seconds")
    rows = [
        (
            chat_id, message_id, user_id, user_name, bought_at,
            item.name, item.category, item.quantity, item.unit, item.price,
            parsed.store, int(item.is_food), int(item.perishable), raw_text, now,
        )
        for item in parsed.items
    ]
    if not rows:
        return 0
    await _db().executemany(
        """
        INSERT INTO purchases
            (chat_id, message_id, user_id, user_name, bought_at,
             name, category, quantity, unit, price,
             store, is_food, perishable, raw_text, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    await _db().commit()
    return len(rows)


async def delete_last_message(chat_id: int) -> tuple[int, float]:
    """Удалить позиции последнего сохранённого сообщения. -> (сколько строк, на какую сумму)."""
    cur = await _db().execute(
        "SELECT message_id FROM purchases WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
        (chat_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return 0, 0.0

    message_id = row["message_id"]
    if message_id is None:
        # На всякий случай: строка без message_id — удаляем ровно её.
        cur = await _db().execute(
            "SELECT id, price FROM purchases WHERE chat_id = ? ORDER BY id DESC LIMIT 1", (chat_id,)
        )
        one = await cur.fetchone()
        await _db().execute("DELETE FROM purchases WHERE id = ?", (one["id"],))
        await _db().commit()
        return 1, one["price"]

    cur = await _db().execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(price), 0) AS total FROM purchases WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
    )
    agg = await cur.fetchone()
    await _db().execute(
        "DELETE FROM purchases WHERE chat_id = ? AND message_id = ?", (chat_id, message_id)
    )
    await _db().commit()
    return agg["n"], agg["total"]


async def mark_consumed(chat_id: int, needle: str) -> int:
    """Отметить продукт как съеденный (поиск по вхождению в название)."""
    now = dt.datetime.now(config.TIMEZONE).isoformat(timespec="seconds")
    cur = await _db().execute(
        """
        UPDATE purchases SET consumed_at = ?
        WHERE chat_id = ? AND is_food = 1 AND consumed_at IS NULL
          AND pylower(name) LIKE '%' || pylower(?) || '%'
        """,
        (now, chat_id, needle),
    )
    await _db().commit()
    return cur.rowcount


# --- чтение -----------------------------------------------------------------

async def category_stats(chat_id: int, since: str, until: str) -> list[tuple[str, float, int]]:
    """Суммы по категориям за период, по убыванию. -> [(категория, сумма, позиций)]"""
    cur = await _db().execute(
        """
        SELECT category, SUM(price) AS total, COUNT(*) AS n
        FROM purchases
        WHERE chat_id = ? AND bought_at BETWEEN ? AND ?
        GROUP BY category
        ORDER BY total DESC
        """,
        (chat_id, since, until),
    )
    return [(r["category"], r["total"], r["n"]) for r in await cur.fetchall()]


async def period_total(chat_id: int, since: str, until: str) -> float:
    cur = await _db().execute(
        "SELECT COALESCE(SUM(price), 0) AS total FROM purchases WHERE chat_id = ? AND bought_at BETWEEN ? AND ?",
        (chat_id, since, until),
    )
    row = await cur.fetchone()
    return row["total"]


async def top_items(chat_id: int, since: str, until: str, limit: int = 10) -> list[tuple[str, float, int]]:
    cur = await _db().execute(
        """
        SELECT name, SUM(price) AS total, COUNT(*) AS n
        FROM purchases
        WHERE chat_id = ? AND bought_at BETWEEN ? AND ?
        GROUP BY pylower(name)
        ORDER BY total DESC
        LIMIT ?
        """,
        (chat_id, since, until, limit),
    )
    return [(r["name"], r["total"], r["n"]) for r in await cur.fetchall()]


async def fridge(chat_id: int, since: str) -> list[Purchase]:
    """Съедобное, купленное не раньше `since` и не отмеченное как съеденное."""
    cur = await _db().execute(
        """
        SELECT * FROM purchases
        WHERE chat_id = ? AND is_food = 1 AND consumed_at IS NULL AND bought_at >= ?
        ORDER BY perishable DESC, bought_at ASC
        """,
        (chat_id, since),
    )
    return [Purchase.from_row(r) for r in await cur.fetchall()]


async def recent(chat_id: int, limit: int) -> list[Purchase]:
    cur = await _db().execute(
        "SELECT * FROM purchases WHERE chat_id = ? ORDER BY bought_at DESC, id DESC LIMIT ?",
        (chat_id, limit),
    )
    return [Purchase.from_row(r) for r in await cur.fetchall()]


async def all_rows(chat_id: int) -> list[aiosqlite.Row]:
    cur = await _db().execute(
        "SELECT * FROM purchases WHERE chat_id = ? ORDER BY bought_at, id", (chat_id,)
    )
    return list(await cur.fetchall())
