"""
llm.py — всё общение бота с нейросетью.

Четыре задачи:
  1. parse_message   — разобрать сообщение из чата в структурированные покупки;
  2. parse_receipt   — прочитать фотографию кассового чека;
  3. ask             — ответить на свободный вопрос по данным о покупках;
  4. suggest_recipes — предложить блюда из того, что лежит в холодильнике.

Какой именно провайдер это делает — Claude, YandexGPT или GigaChat — решает
.env (LLM_PROVIDER и VISION_PROVIDER). Здесь же живут промпты и разбор ответа,
чтобы не дублировать их в каждом провайдере.

Каждая функция возвращает результат и список расходов: у чтения чека через OCR
вызовов два (распознавание + разбор), у остальных — один.
"""
from __future__ import annotations

import logging

import config
import prompts
import providers
from models import ParsedMessage
from providers import LLMError, TransientError
from usage import KIND_ASK, KIND_PARSE, KIND_RECEIPT, KIND_RECIPE, Usage

log = logging.getLogger(__name__)

# Пустой результат — возвращаем его при любой ошибке разбора, чтобы бот не падал в чате.
_NOT_A_PURCHASE = ParsedMessage(
    is_purchase=False, store=None, bought_on=None, items=[], total=None, note=None
)


def chat_provider() -> providers.Provider:
    return providers.get(config.LLM_PROVIDER)


def vision_provider() -> providers.Provider:
    return providers.get(config.VISION_PROVIDER)


async def close() -> None:
    await providers.close_all()


async def parse_message(text: str, today: str) -> tuple[ParsedMessage, list[Usage]]:
    """
    Разобрать сообщение чата.

    Это фоновая работа: человек просто написал в чат, ответа не ждёт. Поэтому
    временные сбои проглатываем (вернём is_purchase=False), а вот ошибки
    настройки бросаем наружу — про них пользователь должен узнать сразу, иначе
    бот молча перестанет записывать покупки.
    """
    try:
        reply = await chat_provider().complete_json(
            system=prompts.PARSER_SYSTEM,
            user=f"Сегодня {today}.\n\nСообщение из чата:\n{text}",
            kind=KIND_PARSE,
        )
    except TransientError as exc:
        log.warning("Разбор сообщения не состоялся: %s", exc)
        return _NOT_A_PURCHASE, []
    except LLMError:
        raise
    except Exception:  # noqa: BLE001 — неожиданный сбой не должен ронять бота
        log.exception("Не удалось разобрать сообщение")
        return _NOT_A_PURCHASE, []

    return _to_parsed(reply.text), [reply.usage]


async def parse_receipt(
    image: bytes,
    media_type: str,
    caption: str | None,
    today: str,
) -> tuple[ParsedMessage, list[Usage]]:
    """
    Прочитать фотографию чека.

    Два пути: модель либо смотрит на картинку сама (Claude, GigaChat), либо
    картинку сначала распознаёт OCR, а модели достаётся текст (YandexGPT).
    """
    provider = vision_provider()
    hint = f"\n\nПодпись к фото от отправителя: {caption}" if caption else ""
    task = (
        f"Сегодня {today}. Разбери этот чек.{hint}\n\n"
        "Если в подписи названа общая сумма, используй её только для проверки: "
        "источник истины — позиции самого чека."
    )

    if provider.supports_images:
        reply = await provider.read_image(
            system=prompts.RECEIPT_SYSTEM,
            user=task,
            image=image,
            media_type=media_type,
            kind=KIND_RECEIPT,
        )
        return _to_parsed(reply.text), [reply.usage]

    if provider.supports_ocr:
        text, ocr_spent = await provider.ocr(image, media_type)
        log.info("OCR распознал %s символов", len(text))
        reply = await provider.read_text(
            system=prompts.RECEIPT_TEXT_SYSTEM,
            user=f"{task}\n\nРаспознанный текст чека:\n{text}",
            kind=KIND_RECEIPT,
        )
        return _to_parsed(reply.text), [ocr_spent, reply.usage]

    raise LLMError(
        f"{provider.name} не умеет читать чеки. Выберите другой VISION_PROVIDER "
        "или выключите READ_RECEIPTS в .env."
    )


async def ask(question: str, context: str) -> tuple[str, list[Usage]]:
    """Свободный вопрос по данным о покупках."""
    return await _chat(
        system=prompts.ASSISTANT_SYSTEM,
        user=f"Данные о покупках семьи:\n\n{context}\n\n---\n\nВопрос: {question}",
        kind=KIND_ASK,
    )


async def suggest_recipes(fridge_text: str, wish: str | None) -> tuple[str, list[Usage]]:
    """Что приготовить из имеющегося."""
    wish_line = f"\n\nПожелание: {wish}" if wish else ""
    return await _chat(
        system=prompts.RECIPE_SYSTEM,
        user=f"Продукты, купленные недавно:\n\n{fridge_text}{wish_line}",
        kind=KIND_RECIPE,
    )


async def _chat(*, system: str, user: str, kind: str) -> tuple[str, list[Usage]]:
    reply = await chat_provider().complete(system=system, user=user, kind=kind)
    answer = reply.text or "Мне нечего добавить по этим данным."
    return answer, [reply.usage]


def _to_parsed(text: str) -> ParsedMessage:
    """Ответ модели -> ParsedMessage. При любой неожиданности — «это не покупка»."""
    if not text:
        return _NOT_A_PURCHASE
    try:
        return ParsedMessage.model_validate_json(text)
    except ValueError:
        log.exception("Не разобрался ответ модели: %.300s", text)
        return _NOT_A_PURCHASE
