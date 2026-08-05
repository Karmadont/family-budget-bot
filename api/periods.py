"""
api/periods.py — периоды для веб-интерфейса.

В боте период приходит словом из команды (`/stats неделя`), и services.parse_period
сразу отдаёт готовую подпись. Приложению нужно другое: фиксированный набор
переключателей, предыдущий период для сравнения «стало/было» и понимание, с каким
шагом рисовать график — по дням или по месяцам.
"""
from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass

from services import MONTHS, today

# Ключи, которые присылает фронтенд. Порядок — порядок кнопок в приложении.
PERIOD_KEYS = ("week", "month", "year", "all")
DEFAULT_PERIOD = "month"


@dataclass(slots=True, frozen=True)
class Period:
    key: str
    since: str        # YYYY-MM-DD включительно
    until: str        # YYYY-MM-DD включительно
    label: str
    step: str         # 'day' | 'month' — с каким шагом рисовать график

    def as_json(self) -> dict:
        return {"key": self.key, "since": self.since, "until": self.until,
                "label": self.label, "step": self.step}


def _month_label(day: dt.date, with_year: bool = False) -> str:
    name = MONTHS[day.month - 1]
    return f"{name} {day.year}" if with_year else name


def resolve(key: str | None, earliest: str | None = None) -> Period:
    """Ключ периода -> границы. Неизвестный ключ и пустой считаем месяцем."""
    key = (key or "").strip().lower()
    if key not in PERIOD_KEYS:
        key = DEFAULT_PERIOD
    now = today()
    until = now.isoformat()

    if key == "week":
        since = now - dt.timedelta(days=6)
        return Period("week", since.isoformat(), until, "за 7 дней", "day")

    if key == "year":
        return Period("year", now.replace(month=1, day=1).isoformat(), until,
                      f"за {now.year} год", "month")

    if key == "all":
        # Без покупок нижняя граница неважна — берём сегодня, чтобы не рисовать
        # пустой график длиной в десятилетия.
        since = earliest or until
        step = "day" if (dt.date.fromisoformat(until) - dt.date.fromisoformat(since)).days <= 62 else "month"
        return Period("all", since, until, "за всё время", step)

    return Period("month", now.replace(day=1).isoformat(), until,
                  f"за {_month_label(now)}", "day")


def previous(period: Period) -> Period | None:
    """
    Предыдущий период того же масштаба — для строчки «было столько-то».

    Для «всего времени» сравнивать не с чем.
    """
    since = dt.date.fromisoformat(period.since)
    until = dt.date.fromisoformat(period.until)

    if period.key == "all":
        return None

    if period.key == "week":
        end = since - dt.timedelta(days=1)
        start = end - dt.timedelta(days=6)
        return Period("week", start.isoformat(), end.isoformat(), "за прошлые 7 дней", "day")

    if period.key == "year":
        year = since.year - 1
        return Period("year", dt.date(year, 1, 1).isoformat(), dt.date(year, 12, 31).isoformat(),
                      f"за {year} год", "month")

    # Месяц: весь предыдущий календарный, но не дальше того же числа, что сейчас.
    # Иначе 5 августа сравнивалось бы с полным июлем, и «стало меньше» было бы
    # неизбежным — а это не новость, а свойство календаря.
    end_month = since - dt.timedelta(days=1)
    last_day = calendar.monthrange(end_month.year, end_month.month)[1]
    start = end_month.replace(day=1)
    end = end_month.replace(day=min(until.day, last_day))
    return Period("month", start.isoformat(), end.isoformat(),
                  f"за {_month_label(end_month)}", "day")
