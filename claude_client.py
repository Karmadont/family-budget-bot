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

import logging

import anthropic
from anthropic import AsyncAnthropic

import config
import prompts
from models import PARSED_MESSAGE_SCHEMA, ParsedMessage

log = logging.getLogger(__name__)

_client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

# Пустой результат — возвращаем его при любой ошибке, чтобы бот не падал в чате.
_NOT_A_PURCHASE = ParsedMessage(is_purchase=False, store=None, bought_on=None, items=[], note=None)


class ClaudeError(RuntimeError):
    """Ошибка обращения к API, которую имеет смысл показать пользователю."""


def _text_of(response: anthropic.types.Message) -> str:
    """Собрать текстовые блоки ответа (пропуская thinking и прочее)."""
    return "".join(block.text for block in response.content if block.type == "text").strip()


async def parse_message(text: str, today: str) -> ParsedMessage:
    """Разобрать сообщение чата. Никогда не бросает — при ошибке вернёт is_purchase=False."""
    try:
        response = await _client.messages.create(
            model=config.CLAUDE_PARSER_MODEL,
            max_tokens=4096,
            system=prompts.PARSER_SYSTEM,
            output_config={
                "format": {"type": "json_schema", "schema": PARSED_MESSAGE_SCHEMA},
                "effort": "low",
            },
            messages=[{"role": "user", "content": f"Сегодня {today}.\n\nСообщение из чата:\n{text}"}],
        )
    except anthropic.APIError:
        log.exception("Claude не смог разобрать сообщение")
        return _NOT_A_PURCHASE

    if response.stop_reason == "refusal":
        log.warning("Модель отказалась разбирать сообщение")
        return _NOT_A_PURCHASE

    try:
        return ParsedMessage.model_validate_json(_text_of(response))
    except ValueError:
        log.exception("Не разобрался ответ модели")
        return _NOT_A_PURCHASE


async def ask(question: str, context: str) -> str:
    """Свободный вопрос по данным о покупках."""
    return await _chat(
        system=prompts.ASSISTANT_SYSTEM,
        user=f"Данные о покупках семьи:\n\n{context}\n\n---\n\nВопрос: {question}",
    )


async def suggest_recipes(fridge_text: str, wish: str | None) -> str:
    """Что приготовить из имеющегося."""
    wish_line = f"\n\nПожелание: {wish}" if wish else ""
    return await _chat(
        system=prompts.RECIPE_SYSTEM,
        user=f"Продукты, купленные недавно:\n\n{fridge_text}{wish_line}",
    )


async def _chat(*, system: str, user: str) -> str:
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
    except anthropic.AuthenticationError as exc:
        raise ClaudeError("Claude не принял ключ — проверьте ANTHROPIC_API_KEY в .env.") from exc
    except anthropic.APIStatusError as exc:
        log.exception("Claude вернул ошибку %s", exc.status_code)
        raise ClaudeError("Claude сейчас недоступен, попробуйте позже.") from exc
    except anthropic.APIConnectionError as exc:
        raise ClaudeError("Не получилось достучаться до Claude — проверьте интернет.") from exc

    if response.stop_reason == "refusal":
        raise ClaudeError("Модель отказалась отвечать на этот вопрос.")

    answer = _text_of(response)
    return answer or "Мне нечего добавить по этим данным."
