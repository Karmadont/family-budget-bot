"""
usage.py — учёт токенов и денег.

YandexGPT возвращает количество токенов в ответе, прайс известен — значит
стоимость можно посчитать точно, а не гадать. Считаем её в момент вызова и
складываем в базу вместе с результатом: цены со временем меняются, и
пересчитывать историю по новому прайсу было бы неверно.

Всё в рублях: Yandex Cloud списывает рубли.
"""
from __future__ import annotations

from dataclasses import dataclass

import config

# Виды вызовов — по ним потом разбивка в /cost.
KIND_PARSE = "parse"
KIND_RECEIPT = "receipt"
KIND_ASK = "ask"
KIND_RECIPE = "recipe"
KIND_ANALYSIS = "analysis"

KIND_LABELS = {
    KIND_PARSE: "разбор покупок",
    KIND_RECEIPT: "чтение чеков",
    KIND_ASK: "вопросы",
    KIND_RECIPE: "рецепты",
    KIND_ANALYSIS: "анализ недели",
}

# Рублей за миллион токенов. У Яндекса ставка одна за вход и выход, и зависит
# она от тарифа и объёма — публичный прайс лишь ориентир. Впишите свои реальные
# ставки в .env (YANDEX_PRICE_*), иначе /cost покажет оценку, а не факт.
# Прайс: https://yandex.cloud/ru/docs/foundation-models/pricing
RATES_PER_1M: dict[str, float] = {
    "yandexgpt-lite": config.YANDEX_PRICE_LITE,
    "yandexgpt":      config.YANDEX_PRICE_PRO,
}


def rate_for(model: str) -> float:
    """Рублей за миллион токенов для модели. modelUri 'yandexgpt-lite/latest' -> ставка."""
    family = model.split("/", 1)[0].split("://")[-1].rsplit("/", 1)[-1].lower()
    rate = RATES_PER_1M.get(family)
    if rate is None:
        # Точного совпадения нет — берём самый длинный подходящий префикс,
        # чтобы 'yandexgpt-lite' не считался по ставке 'yandexgpt'.
        matches = [r for name, r in RATES_PER_1M.items() if family.startswith(name)]
        rate = max(matches) if matches else config.YANDEX_PRICE_PRO
    return rate


@dataclass(frozen=True, slots=True)
class Usage:
    """Расход одного обращения к API, в рублях."""

    kind: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    # Для вызовов, которые тарифицируются не по токенам (распознавание страницы
    # в Yandex Vision OCR): стоимость известна сразу, считать нечего.
    cost_override: float | None = None

    @property
    def cost(self) -> float:
        """Стоимость вызова в рублях."""
        if self.cost_override is not None:
            return self.cost_override
        return (self.input_tokens + self.output_tokens) * rate_for(self.model) / 1_000_000
