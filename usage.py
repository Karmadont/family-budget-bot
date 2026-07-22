"""
usage.py — учёт токенов и денег.

API возвращает количество токенов в каждом ответе (`response.usage`), цены
известны — значит стоимость можно посчитать точно, а не гадать. Считаем её
в момент вызова и складываем в базу вместе с результатом: цены со временем
меняются, и пересчитывать историю по новому прайсу было бы неверно.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

# Виды вызовов — по ним потом разбивка в /cost.
KIND_PARSE = "parse"
KIND_RECEIPT = "receipt"
KIND_ASK = "ask"
KIND_RECIPE = "recipe"

KIND_LABELS = {
    KIND_PARSE: "разбор покупок",
    KIND_RECEIPT: "чтение чеков",
    KIND_ASK: "вопросы",
    KIND_RECIPE: "рецепты",
}

# Долларов за миллион токенов: (вход, выход, чтение кеша, запись кеша на 5 мин).
# Источник: https://platform.claude.com/docs/en/about-claude/pricing
# Сверено 22.07.2026. Если Anthropic поменяет цены — поправьте здесь.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-fable-5":    (10.0, 50.0, 1.00, 12.50),
    "claude-opus-4-8":   (5.0,  25.0, 0.50,  6.25),
    "claude-opus-4-7":   (5.0,  25.0, 0.50,  6.25),
    "claude-opus-4-6":   (5.0,  25.0, 0.50,  6.25),
    "claude-opus-4-5":   (5.0,  25.0, 0.50,  6.25),
    "claude-sonnet-4-6": (3.0,  15.0, 0.30,  3.75),
    "claude-sonnet-4-5": (3.0,  15.0, 0.30,  3.75),
    "claude-haiku-4-5":  (1.0,   5.0, 0.10,  1.25),
}

# Если модель неизвестна — считаем по Opus, чтобы скорее переоценить, чем недооценить.
FALLBACK_PRICE = (5.0, 25.0, 0.50, 6.25)

# У Sonnet 5 действует вводная цена; с 1 сентября 2026 включается обычная.
_SONNET5_INTRO = (2.0, 10.0, 0.20, 2.50)
_SONNET5_REGULAR = (3.0, 15.0, 0.30, 3.75)
_SONNET5_INTRO_UNTIL = dt.date(2026, 8, 31)


def price_for(model: str, on: dt.date | None = None) -> tuple[float, float, float, float]:
    """Цена модели на указанную дату."""
    if model.startswith("claude-sonnet-5"):
        on = on or dt.date.today()
        return _SONNET5_INTRO if on <= _SONNET5_INTRO_UNTIL else _SONNET5_REGULAR
    # Модель может прийти с датой в конце (claude-haiku-4-5-20251001) — обрежем.
    for known in PRICES:
        if model.startswith(known):
            return PRICES[known]
    return FALLBACK_PRICE


@dataclass(frozen=True, slots=True)
class Usage:
    """Расход одного обращения к API."""

    kind: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def cost_usd(self) -> float:
        inp, out, cache_read, cache_write = price_for(self.model)
        return (
            self.input_tokens * inp
            + self.output_tokens * out
            + self.cache_read_tokens * cache_read
            + self.cache_write_tokens * cache_write
        ) / 1_000_000

    @classmethod
    def of(cls, response, kind: str) -> "Usage":
        """
        Собрать из ответа API.

        Модель берём из ответа, а не из конфига: так в логе окажется то, что
        реально отработало. Поля кеша появляются не всегда — отсюда getattr.
        """
        stats = response.usage
        return cls(
            kind=kind,
            model=getattr(response, "model", "unknown"),
            input_tokens=getattr(stats, "input_tokens", 0) or 0,
            output_tokens=getattr(stats, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(stats, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(stats, "cache_creation_input_tokens", 0) or 0,
        )
