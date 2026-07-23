"""
services.py — то, что между базой и телеграмом: периоды, форматирование,
сборка контекста для модели.
"""
from __future__ import annotations

import calendar
import csv
import datetime as dt
import html
import io

import config
import db
import usage as usage_mod

TELEGRAM_LIMIT = 4000  # реальный лимит 4096, оставляем запас на теги


# --- даты и периоды ---------------------------------------------------------

def today() -> dt.date:
    return dt.datetime.now(config.TIMEZONE).date()


def parse_period(arg: str | None) -> tuple[str, str, str]:
    """
    Разобрать аргумент команды в период. -> (since, until, человекочитаемое название)

    Понимает: сегодня, вчера, неделя, месяц, год, всё, а также число дней ('30').
    По умолчанию — текущий календарный месяц.
    """
    now = today()
    end = now.isoformat()
    key = (arg or "").strip().lower()

    if key in ("сегодня", "today", "day"):
        return now.isoformat(), end, "сегодня"
    if key in ("вчера", "yesterday"):
        y = (now - dt.timedelta(days=1)).isoformat()
        return y, y, "вчера"
    if key in ("неделя", "week", "7"):
        return (now - dt.timedelta(days=6)).isoformat(), end, "за 7 дней"
    if key in ("год", "year"):
        return now.replace(month=1, day=1).isoformat(), end, f"за {now.year} год"
    if key in ("всё", "все", "all", "всего"):
        return "0000-01-01", end, "за всё время"
    if key.isdigit() and int(key) > 0:
        days = int(key)
        return (now - dt.timedelta(days=days - 1)).isoformat(), end, f"за {days} дн."

    # По умолчанию и для 'месяц' — текущий календарный месяц.
    return now.replace(day=1).isoformat(), end, f"за {MONTHS[now.month - 1]}"


MONTHS = (
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
)


# --- форматирование ---------------------------------------------------------

def money(value: float) -> str:
    """1234.0 -> '1 234 ₽', 1234.5 -> '1 234,50 ₽'"""
    if abs(value - round(value)) < 0.005:
        body = f"{round(value):,}".replace(",", " ")
    else:
        body = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return f"{body} {config.CURRENCY}"


def esc(text: str | None) -> str:
    return html.escape(text or "", quote=False)


def usd(value: float) -> str:
    """Доллары с разумным числом знаков: $3.42 и $0.63, но $0.0021 для мелочи."""
    if value >= 0.1:
        return f"${value:,.2f}".replace(",", " ")
    if value >= 0.001:
        return f"${value:.4f}"
    return f"${value:.5f}"


def rub(value: float) -> str:
    """Рубли с тем же принципом: 128 ₽, 1,20 ₽, 0,26 ₽, 0,0043 ₽."""
    if value >= 100:
        return f"{value:,.0f} ₽".replace(",", " ")
    if value >= 1:
        return f"{value:,.2f} ₽".replace(",", " ").replace(".", ",")
    # У мелочи хвост из нулей только мешает: 0,2600 -> 0,26
    return f"{value:.4f}".rstrip("0").rstrip(".").replace(".", ",") + " ₽"


def spent(value: float, currency: str) -> str:
    """Стоимость вызовов в валюте провайдера."""
    return rub(value) if currency == usage_mod.RUB else usd(value)


def plural(count: int, one: str, few: str, many: str) -> str:
    """Русское склонение: 1 вызов, 2 вызова, 5 вызовов."""
    tail = abs(count) % 100
    if 11 <= tail <= 14:
        return many
    tail %= 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.0f}K"
    return str(count)


def amount(item: db.Purchase) -> str:
    """'2 кг' или '' — количество позиции, если известно."""
    if item.quantity is None:
        return ""
    qty = f"{item.quantity:g}"
    return f"{qty} {item.unit}".strip() if item.unit else qty


def chunks(text: str, limit: int = TELEGRAM_LIMIT):
    """Порезать длинный ответ на сообщения по границам строк."""
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        yield text[:cut]
        text = text[cut:].lstrip("\n")
    if text:
        yield text


# --- отчёты -----------------------------------------------------------------

async def stats_report(chat_id: int, since: str, until: str, label: str) -> str:
    stats = await db.category_stats(chat_id, since, until)
    if not stats:
        return f"За период <b>{esc(label)}</b> покупок не записано."

    total = sum(row[1] for row in stats)
    lines = [f"<b>Расходы {esc(label)}</b>", f"Всего: <b>{money(total)}</b>", ""]
    for category, subtotal, count in stats:
        share = subtotal / total * 100 if total else 0
        lines.append(f"• {esc(category)} — <b>{money(subtotal)}</b>  ({share:.0f}%, {count} поз.)")

    top = await db.top_items(chat_id, since, until, limit=5)
    if len(top) > 1:
        lines += ["", "<b>Самое дорогое</b>"]
        lines += [f"• {esc(name)} — {money(subtotal)}" for name, subtotal, _ in top]

    return "\n".join(lines)


async def cost_report(chat_id: int, since: str, until: str, label: str) -> str:
    """Сколько потрачено на нейросеть за период — с разбивкой по провайдерам."""
    calls, tok_in, tok_out = await db.usage_totals(chat_id, since, until)
    if not calls:
        return f"За период <b>{esc(label)}</b> обращений к нейросети не было."

    by_provider = await db.usage_by("provider", chat_id, since, until)
    totals = _by_currency(by_provider)
    rubles = _total_rubles(totals)

    lines = [
        f"<b>Расходы на нейросеть {esc(label)}</b>",
        f"Всего: <b>{_amounts(totals)}</b> за {calls} "
        f"{plural(calls, 'вызов', 'вызова', 'вызовов')}",
    ]
    if rubles is None:
        lines.append("<i>Задайте USD_RATE в .env — сведу провайдеров в одну валюту.</i>")
    elif usage_mod.USD in totals:
        lines.append(f"<i>≈ {rub(rubles)} по курсу {config.USD_RATE:g} ₽/$</i>")

    # Ради этой разбивки всё и затевалось: видно, во что обходится каждый провайдер.
    if len(by_provider) > 1:
        lines += ["", "<b>По провайдерам</b>"]
        lines += _breakdown(by_provider, usage_mod.PROVIDER_LABELS)

    lines += ["", "<b>По операциям</b>"]
    lines += _breakdown(
        await db.usage_by("kind", chat_id, since, until), usage_mod.KIND_LABELS
    )

    by_model = await db.usage_by("model", chat_id, since, until)
    if len({model for model, *_ in by_model}) > 1:
        lines += ["", "<b>По моделям</b>"]
        lines += _breakdown(by_model, {})

    lines += ["", f"<i>Токенов: {tokens(tok_in)} вход / {tokens(tok_out)} выход</i>"]

    forecast = _month_forecast(since, until, totals)
    if forecast is not None:
        lines.append(f"<i>При таком темпе за месяц выйдет ~{_amounts(forecast)}</i>")

    return "\n".join(lines)


def _by_currency(rows: list[tuple[str, str, float, int]]) -> dict[str, float]:
    """Суммы из разбивки, сложенные по валютам."""
    totals: dict[str, float] = {}
    for _, currency, cost, _count in rows:
        totals[currency] = totals.get(currency, 0.0) + cost
    return totals


def _amounts(totals: dict[str, float]) -> str:
    """Суммы в разных валютах одной строкой: '128 ₽ + $0.15'."""
    return " + ".join(spent(cost, currency) for currency, cost in totals.items()) or "0 ₽"


def _total_rubles(totals: dict[str, float]) -> float | None:
    """Всё в рублях. None, если есть доллары, а курс не задан."""
    total = 0.0
    for currency, cost in totals.items():
        value = usage_mod.to_rubles(cost, currency)
        if value is None:
            return None
        total += value
    return total


# Курс исключительно для сортировки строк отчёта, когда USD_RATE не задан:
# показывать это число никому не нужно, но доллары и рубли надо как-то
# расставить по убыванию в одном списке.
_SORT_RATE = 100.0


def _breakdown(
    rows: list[tuple[str, str, float, int]], labels: dict[str, str]
) -> list[str]:
    """
    Строки разбивки: одна на значение, даже если оно набралось в разных валютах.

    Из базы приходит по строке на (значение, валюта) — иначе доллары сложились
    бы с рублями. Здесь сводим обратно, чтобы «разбор покупок» не встречался
    в отчёте дважды.
    """
    grouped: dict[str, tuple[dict[str, float], int]] = {}
    for bucket, currency, cost, count in rows:
        costs, calls = grouped.setdefault(bucket, ({}, 0))
        costs[currency] = costs.get(currency, 0.0) + cost
        grouped[bucket] = (costs, calls + count)

    def weight(item: tuple[str, tuple[dict[str, float], int]]) -> float:
        costs, _ = item[1]
        return sum(
            cost * (_SORT_RATE if currency == usage_mod.USD else 1)
            for currency, cost in costs.items()
        )

    lines = []
    for bucket, (costs, calls) in sorted(grouped.items(), key=weight, reverse=True):
        name = labels.get(bucket, bucket)
        lines.append(f"• {esc(name)} — {_amounts(costs)} ({calls})")
    return lines


def _month_forecast(
    since: str, until: str, totals: dict[str, float]
) -> dict[str, float] | None:
    """Прогноз на конец месяца — только если смотрим текущий месяц с его начала."""
    now = today()
    if since != now.replace(day=1).isoformat() or until != now.isoformat():
        return None
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    scale = days_in_month / now.day
    return {currency: cost * scale for currency, cost in totals.items()}


async def fridge_report(chat_id: int) -> str:
    items = await fridge_items(chat_id)
    if not items:
        return (
            f"В холодильнике пусто — за последние {config.FRIDGE_WINDOW_DAYS} дн. "
            "продукты не записывались."
        )

    now = today()
    lines = [f"<b>Скорее всего есть дома</b> (куплено за {config.FRIDGE_WINDOW_DAYS} дн.)", ""]
    warned = False
    for item in items:
        age = (now - dt.date.fromisoformat(item.bought_at)).days
        mark = " ⚠️" if item.perishable and age >= 4 else ""
        warned = warned or bool(mark)
        qty = amount(item)
        qty_part = f" — {qty}" if qty else ""
        age_part = "сегодня" if age == 0 else f"{age} дн. назад"
        lines.append(f"• {esc(item.name)}{qty_part} <i>({age_part})</i>{mark}")

    lines.append("")
    if warned:
        lines.append("⚠️ — лежит уже несколько дней, стоит съесть в первую очередь.")
    lines.append("Съели что-то? <code>/ate молоко</code>")
    return "\n".join(lines)


async def fridge_items(chat_id: int) -> list[db.Purchase]:
    since = (today() - dt.timedelta(days=config.FRIDGE_WINDOW_DAYS)).isoformat()
    return await db.fridge(chat_id, since)


def fridge_as_text(items: list[db.Purchase]) -> str:
    """Список продуктов в виде простого текста для модели."""
    now = today()
    lines = []
    for item in items:
        age = (now - dt.date.fromisoformat(item.bought_at)).days
        qty = amount(item)
        tags = []
        if qty:
            tags.append(qty)
        tags.append(f"куплено {age} дн. назад")
        if item.perishable:
            tags.append("скоропортящееся")
        lines.append(f"- {item.name} ({', '.join(tags)})")
    return "\n".join(lines)


# --- контекст для свободных вопросов ---------------------------------------

async def build_context(chat_id: int) -> str:
    """
    Выжимка из базы, которую бот отдаёт модели вместе с вопросом.

    Держим её компактной: агрегаты за месяц + список последних покупок.
    Весь чат целиком в модель не уходит — только то, что бот распознал как покупки.
    """
    now = today()
    month_start = now.replace(day=1).isoformat()
    week_start = (now - dt.timedelta(days=6)).isoformat()
    end = now.isoformat()

    parts = [f"Сегодня: {end}. Валюта: {config.CURRENCY}."]

    month_stats = await db.category_stats(chat_id, month_start, end)
    if month_stats:
        month_total = sum(row[1] for row in month_stats)
        parts.append(
            f"\nТекущий месяц (с {month_start}), всего {month_total:.0f}:\n"
            + "\n".join(f"- {cat}: {total:.0f} ({n} поз.)" for cat, total, n in month_stats)
        )

    week_total = await db.period_total(chat_id, week_start, end)
    parts.append(f"\nЗа последние 7 дней потрачено: {week_total:.0f}")

    items = await fridge_items(chat_id)
    if items:
        parts.append(f"\nПродукты, купленные за последние {config.FRIDGE_WINDOW_DAYS} дн. "
                     f"(вероятно, дома):\n{fridge_as_text(items)}")

    history = await db.recent(chat_id, config.CONTEXT_PURCHASES_LIMIT)
    if history:
        parts.append(
            "\nПоследние покупки (дата | товар | категория | сумма | кто):\n"
            + "\n".join(
                f"{p.bought_at} | {p.name} | {p.category} | {p.price:.0f} | {p.user_name or '?'}"
                for p in history
            )
        )

    return "\n".join(parts)


# --- экспорт ----------------------------------------------------------------

async def export_csv(chat_id: int) -> bytes:
    rows = await db.all_rows(chat_id)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["дата", "товар", "категория", "количество", "единица",
                     "сумма", "магазин", "кто", "съедено"])
    for row in rows:
        writer.writerow([
            row["bought_at"], row["name"], row["category"], row["quantity"] or "",
            row["unit"] or "", row["price"], row["store"] or "",
            row["user_name"] or "", row["consumed_at"] or "",
        ])
    # utf-8-sig — чтобы Excel не ломал кириллицу.
    return buffer.getvalue().encode("utf-8-sig")
