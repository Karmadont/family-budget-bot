"""
usage.py — учёт токенов и денег.

Каждый провайдер возвращает количество токенов в ответе, прайс известен —
значит стоимость можно посчитать точно, а не гадать. Считаем её в момент
вызова и складываем в базу вместе с результатом: цены со временем меняются,
и пересчитывать историю по новому прайсу было бы неверно.

Валюта у провайдеров разная (Anthropic списывает доллары, Яндекс и Сбер —
рубли), поэтому вместе со стоимостью храним и валюту. Сравнить провайдеров
между собой помогает USD_RATE из .env — см. services.cost_report.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import config

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

# Провайдеры.
CLAUDE = "claude"
YANDEXGPT = "yandexgpt"
GIGACHAT = "gigachat"

PROVIDER_LABELS = {
    CLAUDE: "Claude",
    YANDEXGPT: "YandexGPT",
    GIGACHAT: "GigaChat",
}

USD = "USD"
RUB = "RUB"


@dataclass(frozen=True, slots=True)
class Price:
    """Цена за миллион токенов в валюте провайдера."""

    input: float
    output: float
    cache_read: float = 0.0
    cache_write: float = 0.0
    currency: str = USD


# --- Anthropic --------------------------------------------------------------
# Долларов за миллион токенов: (вход, выход, чтение кеша, запись кеша на 5 мин).
# Источник: https://platform.claude.com/docs/en/about-claude/pricing
# Сверено 22.07.2026. Если Anthropic поменяет цены — поправьте здесь.
CLAUDE_PRICES: dict[str, Price] = {
    "claude-fable-5":    Price(10.0, 50.0, 1.00, 12.50),
    "claude-opus-4-8":   Price(5.0,  25.0, 0.50,  6.25),
    "claude-opus-4-7":   Price(5.0,  25.0, 0.50,  6.25),
    "claude-opus-4-6":   Price(5.0,  25.0, 0.50,  6.25),
    "claude-opus-4-5":   Price(5.0,  25.0, 0.50,  6.25),
    "claude-sonnet-4-6": Price(3.0,  15.0, 0.30,  3.75),
    "claude-sonnet-4-5": Price(3.0,  15.0, 0.30,  3.75),
    "claude-haiku-4-5":  Price(1.0,   5.0, 0.10,  1.25),
}

# Если модель неизвестна — считаем по Opus, чтобы скорее переоценить, чем недооценить.
CLAUDE_FALLBACK = Price(5.0, 25.0, 0.50, 6.25)

# У Sonnet 5 действует вводная цена; с 1 сентября 2026 включается обычная.
_SONNET5_INTRO = Price(2.0, 10.0, 0.20, 2.50)
_SONNET5_REGULAR = Price(3.0, 15.0, 0.30, 3.75)
_SONNET5_INTRO_UNTIL = dt.date(2026, 8, 31)


# --- YandexGPT и GigaChat ---------------------------------------------------
# ВНИМАНИЕ: это ориентировочные прайс-листы. У Яндекса и Сбера цена зависит от
# тарифа, объёма и НДС, а публичные прайсы меняются чаще, чем у Anthropic.
# Свои реальные ставки задайте в .env (YANDEX_PRICE_*, GIGACHAT_PRICE_*) —
# иначе /cost покажет оценку, а не факт.
#
# Яндекс: https://yandex.cloud/ru/docs/foundation-models/pricing
# Сбер:   https://developers.sber.ru/docs/ru/gigachat/api/tariffs
#
# Яндекс не делит цену на вход и выход — ставка одна за все токены.
YANDEX_RATES_PER_1M: dict[str, float] = {
    "yandexgpt-lite": config.YANDEX_PRICE_LITE,
    "yandexgpt":      config.YANDEX_PRICE_PRO,
}

GIGACHAT_RATES_PER_1M: dict[str, tuple[float, float]] = {
    "gigachat-2-max":  (config.GIGACHAT_PRICE_MAX, config.GIGACHAT_PRICE_MAX),
    "gigachat-2-pro":  (config.GIGACHAT_PRICE_PRO, config.GIGACHAT_PRICE_PRO),
    "gigachat-2":      (config.GIGACHAT_PRICE_LITE, config.GIGACHAT_PRICE_LITE),
    "gigachat-max":    (config.GIGACHAT_PRICE_MAX, config.GIGACHAT_PRICE_MAX),
    "gigachat-pro":    (config.GIGACHAT_PRICE_PRO, config.GIGACHAT_PRICE_PRO),
    "gigachat":        (config.GIGACHAT_PRICE_LITE, config.GIGACHAT_PRICE_LITE),
}


def _claude_price(model: str, on: dt.date | None = None) -> Price:
    if model.startswith("claude-sonnet-5"):
        on = on or dt.date.today()
        return _SONNET5_INTRO if on <= _SONNET5_INTRO_UNTIL else _SONNET5_REGULAR
    # Модель может прийти с датой в конце (claude-haiku-4-5-20251001) — обрежем.
    for known, price in CLAUDE_PRICES.items():
        if model.startswith(known):
            return price
    return CLAUDE_FALLBACK


def _yandex_price(model: str) -> Price:
    """У Яндекса в modelUri лежит 'yandexgpt-lite/latest' — берём часть до слэша."""
    family = model.split("/", 1)[0].split("://")[-1].rsplit("/", 1)[-1].lower()
    # Сначала точное совпадение, потом самый длинный подходящий префикс:
    # 'yandexgpt-lite' должен обойти 'yandexgpt'.
    rate = YANDEX_RATES_PER_1M.get(family)
    if rate is None:
        matches = [r for name, r in YANDEX_RATES_PER_1M.items() if family.startswith(name)]
        rate = max(matches) if matches else config.YANDEX_PRICE_PRO
    return Price(rate, rate, currency=RUB)


def _gigachat_price(model: str) -> Price:
    name = model.lower()
    rates = GIGACHAT_RATES_PER_1M.get(name)
    if rates is None:
        matches = [r for known, r in GIGACHAT_RATES_PER_1M.items() if name.startswith(known)]
        # Без точного совпадения берём самый дорогой из подходящих: лучше
        # переоценить расход, чем показать заниженную цифру.
        rates = max(matches) if matches else (config.GIGACHAT_PRICE_MAX,) * 2
    return Price(rates[0], rates[1], currency=RUB)


def price_for(provider: str, model: str) -> Price:
    """Цена провайдера за миллион токенов для конкретной модели."""
    if provider == YANDEXGPT:
        return _yandex_price(model)
    if provider == GIGACHAT:
        return _gigachat_price(model)
    return _claude_price(model)


@dataclass(frozen=True, slots=True)
class Usage:
    """Расход одного обращения к API."""

    kind: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # Для вызовов, которые тарифицируются не по токенам (распознавание страницы
    # в Yandex Vision OCR): стоимость известна сразу, считать нечего.
    cost_override: float | None = None
    currency_override: str | None = None

    @property
    def price(self) -> Price:
        return price_for(self.provider, self.model)

    @property
    def currency(self) -> str:
        return self.currency_override or self.price.currency

    @property
    def cost(self) -> float:
        """Стоимость вызова в валюте провайдера."""
        if self.cost_override is not None:
            return self.cost_override
        price = self.price
        return (
            self.input_tokens * price.input
            + self.output_tokens * price.output
            + self.cache_read_tokens * price.cache_read
            + self.cache_write_tokens * price.cache_write
        ) / 1_000_000


def to_rubles(cost: float, currency: str) -> float | None:
    """Стоимость в рублях. None, если пересчитать нечем (не задан USD_RATE)."""
    if currency == RUB:
        return cost
    if not config.USD_RATE:
        return None
    return cost * config.USD_RATE
