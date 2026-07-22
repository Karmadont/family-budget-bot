"""
claude_client.py — всё общение с Claude API.

Три задачи:
  1. parse_message  — разобрать сообщение из чата в структурированные покупки;
  2. ask            — ответить на свободный вопрос по данным о покупках;
  3. suggest_recipes — предложить блюда из того, что лежит в холодильнике.

Разбор сообщений использует structured outputs (output_config.format): модель
физически не может вернуть невалидный JSON, поэтому парсинг не разваливается
на неожиданных формулировках.
"""
from __future__ import annotations

import base64
import logging

import anthropic
from anthropic import AsyncAnthropic

import config
import prompts
from models import PARSED_MESSAGE_SCHEMA, ParsedMessage
from usage import KIND_ASK, KIND_PARSE, KIND_RECEIPT, KIND_RECIPE, Usage

log = logging.getLogger(__name__)

_client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

# Пустой результат — возвращаем его при любой ошибке, чтобы бот не падал в чате.
_NOT_A_PURCHASE = ParsedMessage(
    is_purchase=False, store=None, bought_on=None, items=[], total=None, note=None
)

_JSON_OUTPUT = {"type": "json_schema", "schema": PARSED_MESSAGE_SCHEMA}


class ClaudeError(RuntimeError):
    """Ошибка, которую должен увидеть человек: чинится настройками или деньгами."""


def _text_of(response: anthropic.types.Message) -> str:
    """Собрать текстовые блоки ответа (пропуская thinking и прочее)."""
    return "".join(block.text for block in response.content if block.type == "text").strip()


def _fatal(exc: anthropic.APIStatusError) -> ClaudeError | None:
    """
    Отличить «сломана настройка» от «временно не повезло».

    Первое надо показать в чате, иначе бот молча перестаёт записывать покупки
    и выглядит просто сломанным. Второе — залогировать и забыть.
    """
    # Текст ошибки лежит то в .message, то в теле ответа — смотрим везде.
    text = f"{getattr(exc, 'message', '')} {getattr(exc, 'body', '')}".lower()
    kind = getattr(exc, "type", "") or ""

    if "credit balance" in text or "billing" in text or kind == "billing_error":
        return ClaudeError(
            "На балансе Anthropic закончились деньги, я не могу обратиться к Claude.\n"
            "Пополните счёт: console.anthropic.com → Plans &amp; Billing."
        )
    if isinstance(exc, anthropic.AuthenticationError):
        return ClaudeError("Claude не принял ключ — проверьте ANTHROPIC_API_KEY в .env.")
    if isinstance(exc, anthropic.PermissionDeniedError):
        return ClaudeError("У ключа нет доступа к этой модели — проверьте .env.")
    if isinstance(exc, anthropic.NotFoundError):
        return ClaudeError(
            "Такой модели нет — проверьте CLAUDE_MODEL / CLAUDE_PARSER_MODEL / "
            "CLAUDE_VISION_MODEL в .env."
        )
    return None


async def parse_message(text: str, today: str) -> tuple[ParsedMessage, Usage | None]:
    """
    Разобрать сообщение чата.

    Временные сбои проглатывает (вернёт is_purchase=False), а вот ошибки настройки
    бросает наружу — про них пользователь должен узнать сразу.

    Вместе с результатом возвращает расход токенов (None, если вызов не состоялся).
    """
    try:
        response = await _client.messages.create(
            model=config.CLAUDE_PARSER_MODEL,
            max_tokens=4096,
            system=prompts.PARSER_SYSTEM,
            output_config={"format": _JSON_OUTPUT, "effort": "low"},
            messages=[{"role": "user", "content": f"Сегодня {today}.\n\nСообщение из чата:\n{text}"}],
        )
    except anthropic.APIStatusError as exc:
        if fatal := _fatal(exc):
            raise fatal from exc
        log.exception("Claude не смог разобрать сообщение (%s)", exc.status_code)
        return _NOT_A_PURCHASE, None
    except anthropic.APIConnectionError:
        log.exception("Нет связи с Claude")
        return _NOT_A_PURCHASE, None

    return _to_parsed(response), Usage.of(response, KIND_PARSE)


async def parse_receipt(
    image: bytes,
    media_type: str,
    caption: str | None,
    today: str,
) -> tuple[ParsedMessage, Usage]:
    """
    Прочитать фотографию чека.

    В отличие от разбора текста здесь включено adaptive thinking: мелкий шрифт,
    скидки и весовые позиции требуют аккуратности, а чеки приходят редко —
    экономить на них смысла нет.
    """
    hint = f"\n\nПодпись к фото от отправителя: {caption}" if caption else ""
    content = [
        # Картинка идёт перед текстом — так модель точнее следует инструкции.
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.standard_b64encode(image).decode("ascii"),
            },
        },
        {
            "type": "text",
            "text": (
                f"Сегодня {today}. Разбери этот чек.{hint}\n\n"
                "Если в подписи названа общая сумма, используй её только для проверки: "
                "источник истины — позиции самого чека."
            ),
        },
    ]

    try:
        response = await _client.messages.create(
            model=config.CLAUDE_VISION_MODEL,
            max_tokens=8192,
            system=prompts.RECEIPT_SYSTEM,
            thinking={"type": "adaptive"},
            output_config={"format": _JSON_OUTPUT, "effort": "medium"},
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.APIStatusError as exc:
        if fatal := _fatal(exc):
            raise fatal from exc
        log.exception("Claude вернул ошибку %s при чтении чека", exc.status_code)
        raise ClaudeError("Не получилось прочитать чек — Claude вернул ошибку.") from exc
    except anthropic.APIConnectionError as exc:
        raise ClaudeError("Не получилось достучаться до Claude — проверьте интернет.") from exc

    if response.stop_reason == "max_tokens":
        log.warning("Чек не поместился в ответ целиком")

    return _to_parsed(response), Usage.of(response, KIND_RECEIPT)


def _to_parsed(response: anthropic.types.Message) -> ParsedMessage:
    """Ответ модели -> ParsedMessage. При любой неожиданности — «это не покупка»."""
    if response.stop_reason == "refusal":
        log.warning("Модель отказалась разбирать сообщение")
        return _NOT_A_PURCHASE
    try:
        return ParsedMessage.model_validate_json(_text_of(response))
    except ValueError:
        log.exception("Не разобрался ответ модели")
        return _NOT_A_PURCHASE


async def ask(question: str, context: str) -> tuple[str, Usage]:
    """Свободный вопрос по данным о покупках."""
    return await _chat(
        system=prompts.ASSISTANT_SYSTEM,
        user=f"Данные о покупках семьи:\n\n{context}\n\n---\n\nВопрос: {question}",
        kind=KIND_ASK,
    )


async def suggest_recipes(fridge_text: str, wish: str | None) -> tuple[str, Usage]:
    """Что приготовить из имеющегося."""
    wish_line = f"\n\nПожелание: {wish}" if wish else ""
    return await _chat(
        system=prompts.RECIPE_SYSTEM,
        user=f"Продукты, купленные недавно:\n\n{fridge_text}{wish_line}",
        kind=KIND_RECIPE,
    )


async def _chat(*, system: str, user: str, kind: str) -> tuple[str, Usage]:
    try:
        response = await _client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=4096,
            system=system,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.RateLimitError as exc:
        raise ClaudeError("Слишком много запросов к Claude, попробуйте через минуту.") from exc
    except anthropic.APIStatusError as exc:
        if fatal := _fatal(exc):
            raise fatal from exc
        log.exception("Claude вернул ошибку %s", exc.status_code)
        raise ClaudeError("Claude сейчас недоступен, попробуйте позже.") from exc
    except anthropic.APIConnectionError as exc:
        raise ClaudeError("Не получилось достучаться до Claude — проверьте интернет.") from exc

    if response.stop_reason == "refusal":
        raise ClaudeError("Модель отказалась отвечать на этот вопрос.")

    answer = _text_of(response) or "Мне нечего добавить по этим данным."
    return answer, Usage.of(response, kind)
