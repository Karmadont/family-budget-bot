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


def in_rubles(value_usd: float) -> str:
    """Приписка с рублями, если в .env задан курс. Иначе пусто."""
    if not config.USD_RATE:
        return ""
    return f" (~{round(value_usd * config.USD_RATE):,} ₽)".replace(",", " ")


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
    """Сколько потрачено на Claude API за период."""
    calls, tok_in, tok_out, total = await db.usage_totals(chat_id, since, until)
    if not calls:
        return f"За период <b>{esc(label)}</b> обращений к Claude не было."

    lines = [
        f"<b>Расходы на Claude {esc(label)}</b>",
        f"Всего: <b>{usd(total)}</b>{in_rubles(total)} за {calls} "
        f"{plural(calls, 'вызов', 'вызова', 'вызовов')}",
        "",
        "<b>По операциям</b>",
    ]
    for kind, cost, count in await db.usage_by("kind", chat_id, since, until):
        name = usage_mod.KIND_LABELS.get(kind, kind)
        lines.append(f"• {esc(name)} — {usd(cost)} ({count})")

    by_model = await db.usage_by("model", chat_id, since, until)
    if len(by_model) > 1:
        lines += ["", "<b>По моделям</b>"]
        lines += [f"• {esc(m)} — {usd(cost)} ({count})" for m, cost, count in by_model]

    lines += ["", f"<i>Токенов: {tokens(tok_in)} вход / {tokens(tok_out)} выход</i>"]

    forecast = _month_forecast(since, until, total)
    if forecast is not None:
        lines.append(f"<i>При таком темпе за месяц выйдет ~{usd(forecast)}{in_rubles(forecast)}</i>")

    return "\n".join(lines)


def _month_forecast(since: str, until: str, spent: float) -> float | None:
    """Прогноз на конец месяца — только если смотрим текущий месяц с его начала."""
    now = today()
    if since != now.replace(day=1).isoformat() or until != now.isoformat():
        return None
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    return spent / now.day * days_in_month


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
