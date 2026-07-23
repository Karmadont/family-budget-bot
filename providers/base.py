"""
providers/base.py — что должен уметь провайдер нейросети.

Три задачи бота сводятся к трём операциям:
  complete      — свободный текстовый ответ (вопросы, рецепты);
  complete_json — ответ строго по JSON-схеме (разбор покупок);
  и чтение чека, у которого два разных пути:
    read_image   — модель сама смотрит на картинку (Claude, GigaChat);
    ocr          — картинку распознаёт отдельный сервис, модели достаётся
                   текст (YandexGPT: текстовая модель картинок не видит).

Какой путь доступен — говорят флаги supports_images / supports_ocr. Выбор
делает llm.py, чтобы промпты не расползались по провайдерам.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from usage import Usage


class LLMError(RuntimeError):
    """Ошибка, которую должен увидеть человек: чинится настройками или деньгами."""


class TransientError(LLMError):
    """
    Временно не повезло: сеть, лимит запросов, 5xx у провайдера.

    Отличается от остальных LLMError тем, что при фоновом разборе покупок бот
    про неё молчит. Ругаться на каждый сетевой чих в чате, где люди просто
    пишут о покупках, — худшее из зол: настоящую поломку в этом шуме не увидят.
    Там, где человек ждёт ответа (вопрос, рецепт, чек), она всё равно покажется.
    """


@dataclass(frozen=True, slots=True)
class Reply:
    """Ответ модели вместе с ценой вызова."""

    text: str
    usage: Usage


class Provider(ABC):
    """Общий интерфейс. Все методы бросают LLMError на ошибках настройки."""

    name: str = ""
    # Модель принимает картинку напрямую.
    supports_images: bool = False
    # Есть отдельный сервис распознавания текста с картинки.
    supports_ocr: bool = False

    @property
    def reads_receipts(self) -> bool:
        return self.supports_images or self.supports_ocr

    @abstractmethod
    async def complete(self, *, system: str, user: str, kind: str) -> Reply:
        """Свободный ответ обычной моделью."""

    @abstractmethod
    async def complete_json(self, *, system: str, user: str, kind: str) -> Reply:
        """Ответ строго по схеме PARSED_MESSAGE_SCHEMA, моделью-парсером."""

    async def read_image(
        self, *, system: str, user: str, image: bytes, media_type: str, kind: str
    ) -> Reply:
        """Разобрать картинку в JSON по схеме. Только если supports_images."""
        raise LLMError(f"{self.name} не умеет смотреть на картинки.")

    async def read_text(self, *, system: str, user: str, kind: str) -> Reply:
        """
        Разобрать в JSON текст чека, полученный из ocr().

        По умолчанию это обычный JSON-вызов; провайдер может переопределить,
        если для чеков у него отдельная модель.
        """
        return await self.complete_json(system=system, user=user, kind=kind)

    async def ocr(self, image: bytes, media_type: str) -> tuple[str, Usage]:
        """Распознать текст на картинке. Только если supports_ocr."""
        raise LLMError(f"У {self.name} нет распознавания текста с картинок.")

    async def aclose(self) -> None:
        """Закрыть сетевые соединения при остановке бота."""


def strip_json(text: str) -> str:
    """
    Выковырять JSON из ответа модели.

    Claude и YandexGPT возвращают чистый JSON — им это не нужно. GigaChat же
    иногда добавляет ```json-обёртку или вежливое предисловие, и без такой
    чистки разбор ломается на ровном месте.
    """
    text = text.strip()
    if text.startswith("```"):
        # ```json\n{...}\n``` -> {...}
        text = text.split("\n", 1)[-1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
        text = text.strip()

    start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
    if start == -1:
        return text
    end = max(text.rfind("}"), text.rfind("]"))
    return text[start : end + 1] if end > start else text[start:]
