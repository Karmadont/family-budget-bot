"""
db.py — хранилище покупок на SQLite.

Одна строка = одна позиция чека. Сообщение из чата может дать несколько строк,
они связаны общим message_id (это же позволяет откатить последнюю запись целиком).
"""
from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable
from dataclasses import dataclass

import aiosqlite

import config
from models import ParsedMessage
from usage import Usage

log = logging.getLogger(__name__)

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
    created_at  TEXT    NOT NULL,
    -- 1 = модель выбирала категорию или цену наугад, человеку стоит проверить.
    needs_review INTEGER NOT NULL DEFAULT 0,
    -- Откуда запись: chat (сообщение), receipt (фото чека), import (выгрузка).
    source      TEXT    NOT NULL DEFAULT 'chat'
);

CREATE INDEX IF NOT EXISTS idx_purchases_chat_date ON purchases (chat_id, bought_at);
CREATE INDEX IF NOT EXISTS idx_purchases_chat_cat  ON purchases (chat_id, category);
CREATE INDEX IF NOT EXISTS idx_purchases_fridge    ON purchases (chat_id, is_food, consumed_at);

-- Заданные, но пока не отвеченные уточняющие вопросы. Живут до ответа человека
-- либо до истечения PENDING_TTL_DAYS — брошенные вопросы копиться не должны.
CREATE TABLE IF NOT EXISTS pending (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id        INTEGER NOT NULL,
    message_id     INTEGER,             -- исходное сообщение человека
    ask_message_id INTEGER,             -- сообщение бота с вопросом (по нему ловим ответ)
    user_id        INTEGER,
    user_name      TEXT,
    raw_text       TEXT    NOT NULL,    -- что человек написал изначально
    question       TEXT    NOT NULL,
    created_at     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_ask  ON pending (chat_id, ask_message_id);
CREATE INDEX IF NOT EXISTS idx_pending_chat ON pending (chat_id, id);

-- Какие сообщения из выгрузки уже прогонялись через разбор. Нужна отдельно от
-- purchases: сообщение могло оказаться не покупкой и не оставить ни одной строки,
-- а платный разбор для него всё равно был. Без этой таблицы повторный запуск
-- импорта заново платил бы за всю болтовню в чате.
CREATE TABLE IF NOT EXISTS import_log (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    at         TEXT    NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);

-- Расход на YandexGPT в рублях. cost храним посчитанным: цены меняются, и
-- пересчитывать прошлое по новому прайсу было бы неверно.
CREATE TABLE IF NOT EXISTS usage_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id       INTEGER NOT NULL,
    at            TEXT    NOT NULL,          -- ISO timestamp
    day           TEXT    NOT NULL,          -- YYYY-MM-DD, для группировки
    kind          TEXT    NOT NULL,          -- parse | receipt | ask | recipe | analysis
    model         TEXT    NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost          REAL    NOT NULL           -- рубли
);

CREATE INDEX IF NOT EXISTS idx_usage_chat_day ON usage_log (chat_id, day);
"""

# Индексы по колонкам, которых в старых базах не было: их можно создавать только
# после того, как _add_missing_columns() эти колонки допишет.
LATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_purchases_review ON purchases (chat_id, needs_review);
"""

# Индексы под защиту от повторной записи. Обе колонки существуют с первого
# релиза, поэтому их можно создавать вместе с остальной схемой.
DEDUP_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_purchases_chat_msg     ON purchases (chat_id, message_id);
CREATE INDEX IF NOT EXISTS idx_purchases_chat_created ON purchases (chat_id, created_at);
"""

# Колонки, добавленные после первого релиза: (таблица, имя, определение).
# ALTER TABLE ADD COLUMN в SQLite не умеет IF NOT EXISTS, поэтому сверяемся с PRAGMA.
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("purchases", "needs_review", "INTEGER NOT NULL DEFAULT 0"),
    ("purchases", "source", "TEXT NOT NULL DEFAULT 'chat'"),
)

# Столбцы, по которым разрешено группировать расход в /cost.
USAGE_BUCKETS = ("kind", "model")

# Что разрешено править через приложение. Список закрытый: имена столбцов идут
# в SQL, и принимать их из запроса без проверки нельзя.
EDITABLE_FIELDS = frozenset({
    "name", "category", "price", "quantity", "unit", "bought_at",
    "store", "is_food", "perishable", "needs_review",
})

# Сколько дней ждать ответа на уточняющий вопрос.
PENDING_TTL_DAYS = 7

# Насколько дата покупки может отстоять от даты сообщения. Модель извлекает её из
# текста («вчера», «в субботу»), и обычно это несколько дней назад. Значение вне
# окна означает, что модель дату придумала, — такую покупку лучше записать на дату
# сообщения, чем отправить в 2031 год, где её никто не найдёт.
BOUGHT_AT_MAX_DAYS_BACK = 400
BOUGHT_AT_MAX_DAYS_AHEAD = 1


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
    needs_review: bool
    source: str

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
            needs_review=bool(row["needs_review"]),
            source=row["source"],
        )

    def as_json(self) -> dict:
        """Строка для приложения. Ключи совпадают с именами столбцов — так проще
        сверять, что уходит наружу, а что правится через PATCH."""
        return {
            "id": self.id,
            "bought_at": self.bought_at,
            "name": self.name,
            "category": self.category,
            "quantity": self.quantity,
            "unit": self.unit,
            "price": self.price,
            "store": self.store,
            "is_food": self.is_food,
            "perishable": self.perishable,
            "user_name": self.user_name,
            "needs_review": self.needs_review,
            "source": self.source,
        }


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

    old_cols = await _park_legacy_usage_log()
    await _conn.executescript(SCHEMA)
    if old_cols is not None:
        await _restore_legacy_usage_log(old_cols)
    # Порядок важен: сначала догнать колонки в уже существующих базах, и только
    # потом строить индексы, которые на эти колонки ссылаются.
    await _add_missing_columns()
    await _conn.executescript(LATE_INDEXES)
    await _conn.executescript(DEDUP_INDEXES)
    await _conn.commit()


async def _add_missing_columns() -> None:
    """Дописать колонки, появившиеся после первого релиза, в старую базу."""
    for table, column, definition in ADDED_COLUMNS:
        cur = await _conn.execute(f"PRAGMA table_info({table})")
        existing = {row["name"] for row in await cur.fetchall()}
        if not existing or column in existing:
            continue
        log.info("Миграция базы: добавляю %s.%s", table, column)
        await _conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


async def _park_legacy_usage_log() -> set[str] | None:
    """
    Отодвинуть таблицу расходов от прежних версий, если её форма устарела.

    До перехода на один YandexGPT в usage_log были столбцы под разных провайдеров
    (provider, currency) и кеш токенов, а стоимость лежала то в cost, то в cost_usd.
    Переименовываем таблицу, чтобы SCHEMA создала свежую, а данные перельём следом.
    -> набор столбцов старой таблицы, либо None, если миграция не нужна.

    Индекс сносим руками: при переименовании таблицы имя индекса не меняется,
    и `CREATE INDEX IF NOT EXISTS` молча пропустил бы новый.
    """
    cur = await _conn.execute("PRAGMA table_info(usage_log)")
    columns = {row["name"] for row in await cur.fetchall()}
    up_to_date = "cost" in columns and not columns & {"provider", "currency", "cache_read_tokens"}
    if not columns or up_to_date:
        return None

    await _conn.execute("DROP INDEX IF EXISTS idx_usage_chat_day")
    await _conn.execute("ALTER TABLE usage_log RENAME TO usage_log_old")
    return columns


async def _restore_legacy_usage_log(columns: set[str]) -> None:
    """
    Перелить старые записи в новую таблицу.

    Стоимость берём из того столбца, что был (cost или cost_usd). Прежние записи
    Claude были в долларах — они останутся в рублёвом столбце как есть; суммы там
    копеечные и историю расхода на API это не искажает сколько-нибудь заметно.
    """
    cost_col = "cost" if "cost" in columns else "cost_usd"
    await _conn.execute(
        f"""
        INSERT INTO usage_log
            (chat_id, at, day, kind, model, input_tokens, output_tokens, cost)
        SELECT chat_id, at, day, kind, model, input_tokens, output_tokens, {cost_col}
        FROM usage_log_old
        """
    )
    await _conn.execute("DROP TABLE usage_log_old")


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

def _sane_date(from_model: str | None, fallback: str) -> str:
    """
    Дата покупки: из текста, если она правдоподобна, иначе дата сообщения.

    Модель иногда выдаёт дату с выдуманным годом — особенно когда в тексте
    «3 марта» без года. При импорте старой переписки это особенно заметно:
    покупка 2025 года уезжает в 2026-й и пропадает из статистики.
    """
    if not from_model:
        return fallback
    try:
        guessed = dt.date.fromisoformat(from_model)
        anchor = dt.date.fromisoformat(fallback)
    except ValueError:
        log.info("Модель вернула дату %r — беру дату сообщения", from_model)
        return fallback

    shift = (guessed - anchor).days
    if -BOUGHT_AT_MAX_DAYS_BACK <= shift <= BOUGHT_AT_MAX_DAYS_AHEAD:
        return from_model
    log.info("Дата %s далеко от %s (%+d дн.) — беру дату сообщения", from_model, fallback, shift)
    return fallback

async def save_parsed(
    *,
    chat_id: int,
    message_id: int | None,
    user_id: int | None,
    user_name: str | None,
    raw_text: str,
    parsed: ParsedMessage,
    default_date: str,
    source: str = "chat",
) -> int:
    """Сохранить все позиции разобранного сообщения. Возвращает количество строк."""
    bought_at = _sane_date(parsed.bought_on, default_date)
    now = dt.datetime.now(config.TIMEZONE).isoformat(timespec="seconds")
    rows = [
        (
            chat_id, message_id, user_id, user_name, bought_at,
            item.name, item.category, item.quantity, item.unit, item.price,
            parsed.store, int(item.is_food), int(item.perishable), raw_text, now,
            int(item.uncertain), source,
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
             store, is_food, perishable, raw_text, created_at,
             needs_review, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    await _db().commit()
    return len(rows)


async def message_saved(chat_id: int, message_id: int | None) -> bool:
    """
    Записано ли уже что-нибудь по этому сообщению.

    Telegram пересылает апдейт заново, если бот не успел подтвердить его получение
    — а при обрывах связи это ровно то, что происходит. Без проверки одна и та же
    покупка попала бы в статистику дважды, и ещё дважды был бы оплачен разбор.
    """
    if message_id is None:
        return False
    cur = await _db().execute(
        "SELECT 1 FROM purchases WHERE chat_id = ? AND message_id = ? LIMIT 1",
        (chat_id, message_id),
    )
    return await cur.fetchone() is not None


def _normalized(text: str) -> str:
    """Текст без разницы в регистре и пробелах — для сравнения двух сообщений."""
    return " ".join(text.lower().split())


async def recent_twin(chat_id: int, raw_text: str, within_min: int) -> int | None:
    """
    Найти недавнюю запись с тем же текстом. -> message_id или None.

    Это второй слой защиты, для случая, когда повторяется не Telegram, а человек:
    подтверждение не дошло, он решил, что бот не услышал, и написал то же самое
    ещё раз. Сообщение новое, message_id другой, а покупка та же.

    Окно нарочно короткое: купить молоко дважды за день — обычное дело, дважды за
    десять минут и теми же словами — почти наверняка повтор.
    """
    if within_min <= 0:
        return None
    since = (dt.datetime.now(config.TIMEZONE) - dt.timedelta(minutes=within_min))
    cur = await _db().execute(
        """
        SELECT message_id, raw_text FROM purchases
        WHERE chat_id = ? AND created_at >= ? AND message_id IS NOT NULL
        ORDER BY id DESC
        """,
        (chat_id, since.isoformat(timespec="seconds")),
    )
    needle = _normalized(raw_text)
    for row in await cur.fetchall():
        if row["raw_text"] and _normalized(row["raw_text"]) == needle:
            return row["message_id"]
    return None


async def rows_for_message(chat_id: int, message_id: int) -> list[Purchase]:
    """Позиции, записанные по одному сообщению — чтобы переслать подтверждение."""
    cur = await _db().execute(
        "SELECT * FROM purchases WHERE chat_id = ? AND message_id = ? ORDER BY id",
        (chat_id, message_id),
    )
    return [Purchase.from_row(row) for row in await cur.fetchall()]


async def known_message_ids(chat_id: int) -> set[int]:
    """
    Сообщения, которые импорту трогать не нужно.

    Это объединение двух множеств: те, что уже дали покупки, и те, что разбор
    прошли и покупками не оказались. Второе нужно, чтобы повторный запуск не
    оплачивал разбор болтовни заново.

    Отдаём множеством: проверять предстоит тысячи id, и запрос на каждый был бы
    заметно медленнее одного прохода по индексу.
    """
    cur = await _db().execute(
        """
        SELECT message_id FROM purchases WHERE chat_id = ? AND message_id IS NOT NULL
        UNION
        SELECT message_id FROM import_log WHERE chat_id = ?
        """,
        (chat_id, chat_id),
    )
    return {row["message_id"] for row in await cur.fetchall()}


async def mark_imported(chat_id: int, message_ids: Iterable[int]) -> None:
    """Отметить сообщения как уже прогнанные через разбор."""
    now = dt.datetime.now(config.TIMEZONE).isoformat(timespec="seconds")
    rows = [(chat_id, message_id, now) for message_id in message_ids]
    if not rows:
        return
    # INSERT OR IGNORE: при обрыве и повторном запуске часть id уже будет записана.
    await _db().executemany(
        "INSERT OR IGNORE INTO import_log (chat_id, message_id, at) VALUES (?, ?, ?)", rows
    )
    await _db().commit()


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


async def period_summary(chat_id: int, since: str, until: str) -> tuple[float, int, int]:
    """Итог периода одним запросом. -> (сумма, позиций, дней с покупками)"""
    cur = await _db().execute(
        """
        SELECT COALESCE(SUM(price), 0) AS total,
               COUNT(*)               AS items,
               COUNT(DISTINCT bought_at) AS days
        FROM purchases
        WHERE chat_id = ? AND bought_at BETWEEN ? AND ?
        """,
        (chat_id, since, until),
    )
    row = await cur.fetchone()
    return row["total"], row["items"], row["days"]


async def daily_totals(chat_id: int, since: str, until: str) -> list[tuple[str, float]]:
    """Суммы по дням для графика. Дни без покупок не возвращаются — их дорисует фронтенд."""
    cur = await _db().execute(
        """
        SELECT bought_at AS day, SUM(price) AS total
        FROM purchases
        WHERE chat_id = ? AND bought_at BETWEEN ? AND ?
        GROUP BY bought_at
        ORDER BY bought_at
        """,
        (chat_id, since, until),
    )
    return [(r["day"], r["total"]) for r in await cur.fetchall()]


async def first_purchase_date(chat_id: int) -> str | None:
    """Дата самой ранней покупки — нижняя граница для периода «всё время»."""
    cur = await _db().execute(
        "SELECT MIN(bought_at) AS first FROM purchases WHERE chat_id = ?", (chat_id,)
    )
    row = await cur.fetchone()
    return row["first"]


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


async def distinct_chats() -> list[int]:
    """Чаты, где записана хоть одна покупка — кому слать еженедельный дайджест."""
    cur = await _db().execute("SELECT DISTINCT chat_id FROM purchases")
    return [r["chat_id"] for r in await cur.fetchall()]


async def log_usage(chat_id: int, entries: Iterable[Usage]) -> None:
    """Записать расход обращений к API (у чтения чека через OCR их два)."""
    now = dt.datetime.now(config.TIMEZONE)
    rows = [
        (
            chat_id, now.isoformat(timespec="seconds"), now.date().isoformat(),
            entry.kind, entry.model, entry.input_tokens, entry.output_tokens, entry.cost,
        )
        for entry in entries
    ]
    if not rows:
        return
    await _db().executemany(
        """
        INSERT INTO usage_log
            (chat_id, at, day, kind, model, input_tokens, output_tokens, cost)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    await _db().commit()


async def usage_totals(chat_id: int, since: str, until: str) -> tuple[int, int, int, float]:
    """Итог за период. -> (вызовов, входных токенов, выходных, рублей)"""
    cur = await _db().execute(
        """
        SELECT COUNT(*) AS calls,
               COALESCE(SUM(input_tokens), 0) AS tin,
               COALESCE(SUM(output_tokens), 0) AS tout,
               COALESCE(SUM(cost), 0) AS cost
        FROM usage_log
        WHERE chat_id = ? AND day BETWEEN ? AND ?
        """,
        (chat_id, since, until),
    )
    row = await cur.fetchone()
    return row["calls"], row["tin"], row["tout"], row["cost"]


async def usage_by(
    column: str, chat_id: int, since: str, until: str
) -> list[tuple[str, float, int]]:
    """Разбивка расхода по kind или model. -> [(значение, рублей, вызовов)]"""
    if column not in USAGE_BUCKETS:  # column идёт в SQL — только из белого списка
        raise ValueError(f"нельзя группировать по {column!r}")
    cur = await _db().execute(
        f"""
        SELECT {column} AS bucket, SUM(cost) AS cost, COUNT(*) AS calls
        FROM usage_log
        WHERE chat_id = ? AND day BETWEEN ? AND ?
        GROUP BY {column}
        ORDER BY cost DESC
        """,
        (chat_id, since, until),
    )
    return [(r["bucket"], r["cost"], r["calls"]) for r in await cur.fetchall()]


# --- лента покупок и правка -------------------------------------------------

async def purchases_page(
    chat_id: int,
    *,
    since: str | None = None,
    until: str | None = None,
    category: str | None = None,
    review_only: bool = False,
    search: str | None = None,
    cursor: tuple[str, int] | None = None,
    limit: int = 50,
) -> list[Purchase]:
    """
    Страница списка покупок, новые сверху.

    Листаем «по ключу» (bought_at, id), а не через OFFSET: пока человек смотрит
    список, бот продолжает писать новые строки, и OFFSET начал бы то пропускать
    записи, то показывать их дважды.
    """
    where = ["chat_id = ?"]
    args: list = [chat_id]

    if since:
        where.append("bought_at >= ?")
        args.append(since)
    if until:
        where.append("bought_at <= ?")
        args.append(until)
    if category:
        where.append("category = ?")
        args.append(category)
    if review_only:
        where.append("needs_review = 1")
    if search:
        where.append("pylower(name) LIKE '%' || pylower(?) || '%'")
        args.append(search)
    if cursor is not None:
        where.append("(bought_at < ? OR (bought_at = ? AND id < ?))")
        args.extend([cursor[0], cursor[0], cursor[1]])

    cur = await _db().execute(
        f"""
        SELECT * FROM purchases
        WHERE {' AND '.join(where)}
        ORDER BY bought_at DESC, id DESC
        LIMIT ?
        """,
        (*args, limit),
    )
    return [Purchase.from_row(r) for r in await cur.fetchall()]


async def get_purchase(chat_id: int, purchase_id: int) -> Purchase | None:
    """Одна покупка. chat_id в условии обязателен — это проверка прав, не фильтр."""
    cur = await _db().execute(
        "SELECT * FROM purchases WHERE id = ? AND chat_id = ?", (purchase_id, chat_id)
    )
    row = await cur.fetchone()
    return Purchase.from_row(row) if row is not None else None


async def update_purchase(chat_id: int, purchase_id: int, changes: dict) -> bool:
    """
    Изменить поля покупки. -> была ли строка изменена.

    Имена полей сверяются с EDITABLE_FIELDS: они подставляются в SQL, и доверять
    тому, что пришло из запроса, нельзя. Значения идут параметрами.
    """
    fields = {k: v for k, v in changes.items() if k in EDITABLE_FIELDS}
    if not fields:
        return False

    assignments = ", ".join(f"{name} = ?" for name in fields)
    cur = await _db().execute(
        f"UPDATE purchases SET {assignments} WHERE id = ? AND chat_id = ?",
        (*fields.values(), purchase_id, chat_id),
    )
    await _db().commit()
    return cur.rowcount > 0


async def delete_purchase(chat_id: int, purchase_id: int) -> bool:
    cur = await _db().execute(
        "DELETE FROM purchases WHERE id = ? AND chat_id = ?", (purchase_id, chat_id)
    )
    await _db().commit()
    return cur.rowcount > 0


async def review_count(chat_id: int) -> int:
    """Сколько записей ждут проверки — для счётчика в приложении."""
    cur = await _db().execute(
        "SELECT COUNT(*) AS n FROM purchases WHERE chat_id = ? AND needs_review = 1", (chat_id,)
    )
    row = await cur.fetchone()
    return row["n"]


# --- уточняющие вопросы -----------------------------------------------------

async def add_pending(
    *,
    chat_id: int,
    message_id: int | None,
    ask_message_id: int | None,
    user_id: int | None,
    user_name: str | None,
    raw_text: str,
    question: str,
) -> int:
    now = dt.datetime.now(config.TIMEZONE).isoformat(timespec="seconds")
    cur = await _db().execute(
        """
        INSERT INTO pending
            (chat_id, message_id, ask_message_id, user_id, user_name, raw_text, question, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (chat_id, message_id, ask_message_id, user_id, user_name, raw_text, question, now),
    )
    await _db().commit()
    return cur.lastrowid


async def pending_by_ask(chat_id: int, ask_message_id: int) -> aiosqlite.Row | None:
    """Найти вопрос, на который человек ответил через reply."""
    cur = await _db().execute(
        "SELECT * FROM pending WHERE chat_id = ? AND ask_message_id = ?",
        (chat_id, ask_message_id),
    )
    return await cur.fetchone()


async def pending_get(chat_id: int, pending_id: int) -> aiosqlite.Row | None:
    cur = await _db().execute(
        "SELECT * FROM pending WHERE id = ? AND chat_id = ?", (pending_id, chat_id)
    )
    return await cur.fetchone()


async def pending_list(chat_id: int, limit: int = 20) -> list[aiosqlite.Row]:
    cur = await _db().execute(
        "SELECT * FROM pending WHERE chat_id = ? ORDER BY id DESC LIMIT ?", (chat_id, limit)
    )
    return list(await cur.fetchall())


async def drop_pending(pending_id: int) -> None:
    await _db().execute("DELETE FROM pending WHERE id = ?", (pending_id,))
    await _db().commit()


async def purge_pending() -> int:
    """Убрать вопросы, на которые так и не ответили. -> сколько удалено."""
    edge = (dt.datetime.now(config.TIMEZONE) - dt.timedelta(days=PENDING_TTL_DAYS))
    cur = await _db().execute(
        "DELETE FROM pending WHERE created_at < ?", (edge.isoformat(timespec="seconds"),)
    )
    await _db().commit()
    return cur.rowcount


async def all_rows(chat_id: int) -> list[aiosqlite.Row]:
    cur = await _db().execute(
        "SELECT * FROM purchases WHERE chat_id = ? ORDER BY bought_at, id", (chat_id,)
    )
    return list(await cur.fetchall())
