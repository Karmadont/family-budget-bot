"""
models.py — схемы данных, которые модель обязана вернуть.

JSON Schema отсюда уходит в YandexGPT через поле json_schema. Это гарантирует,
что ответ распарсится, и нам не нужно вылавливать JSON из текста регулярками.
"""
from __future__ import annotations

import copy
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

# Фиксированный список категорий. Модель обязана выбрать одну из них —
# иначе статистика превратится в кашу из синонимов.
# Хотите свои категории — правьте здесь, база подхватит автоматически.
Category = Literal[
    "мясо и рыба",
    "овощи и фрукты",
    "молочное и яйца",
    "хлеб и выпечка",
    "бакалея и крупы",
    "напитки",
    "сладости и снеки",
    "заморозка и полуфабрикаты",
    "кафе и доставка",
    "бытовая химия",
    "дом и хозтовары",
    "здоровье и аптека",
    "красота и уход",
    "транспорт",
    "развлечения",
    "одежда и обувь",
    "подписки и связь",
    "детское",
    "питомцы",
    "прочее",
]

CATEGORIES: tuple[str, ...] = get_args(Category)


class PurchaseItem(BaseModel):
    """Одна позиция чека."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Название товара в именительном падеже, единственном числе: 'молоко', 'куриное филе'.")
    category: Category = Field(description="Категория строго из списка.")
    quantity: float | None = Field(description="Количество, если указано в сообщении. Иначе null.")
    unit: str | None = Field(description="Единица измерения: 'шт', 'кг', 'г', 'л', 'мл', 'упак'. Иначе null.")
    price: float = Field(description="Итоговая стоимость этой позиции целиком (не цена за единицу).")
    is_food: bool = Field(description="true, если это еда или напиток, который можно съесть/выпить.")
    perishable: bool = Field(description="true, если продукт скоропортящийся и хранится в холодильнике.")


class ParsedMessage(BaseModel):
    """Результат разбора одного сообщения из чата."""

    model_config = ConfigDict(extra="forbid")

    is_purchase: bool = Field(description="true, если сообщение сообщает о совершённой покупке с ценами.")
    store: str | None = Field(description="Магазин, если упомянут. Иначе null.")
    bought_on: str | None = Field(description="Дата покупки YYYY-MM-DD, если её можно понять из текста ('вчера', '3 марта'). Иначе null.")
    items: list[PurchaseItem] = Field(description="Список позиций. Пустой список, если это не покупка.")
    total: float | None = Field(description="Итоговая сумма, если она явно названа (строка ИТОГО в чеке или сумма в тексте). Иначе null.")
    note: str | None = Field(description="Короткий комментарий, если что-то осталось непонятным. Иначе null.")


def _inline_refs(node: Any, defs: dict) -> Any:
    """Подставить определения из $defs вместо ссылок $ref."""
    if isinstance(node, list):
        return [_inline_refs(item, defs) for item in node]
    if not isinstance(node, dict):
        return node

    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        target = defs.get(ref.rsplit("/", 1)[-1], {})
        resolved = _inline_refs(copy.deepcopy(target), defs)
        # Соседи $ref (title, description) в JSON Schema дополняют цель.
        extras = {k: v for k, v in node.items() if k != "$ref"}
        return {**resolved, **_inline_refs(extras, defs)} if extras else resolved

    return {k: _inline_refs(v, defs) for k, v in node.items() if k != "$defs"}


# Схема для json_schema. extra="forbid" даёт additionalProperties: false,
# отсутствие значений по умолчанию — полный required. YandexGPT ссылки $ref
# понимает плохо, поэтому отдаём схему с развёрнутыми вложенными объектами.
_RAW_SCHEMA: dict = ParsedMessage.model_json_schema()
PARSED_MESSAGE_SCHEMA: dict = _inline_refs(_RAW_SCHEMA, _RAW_SCHEMA.get("$defs", {}))
