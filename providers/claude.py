"""
providers/claude.py — Claude API (Anthropic).

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
from models import PARSED_MESSAGE_SCHEMA
from providers.base import LLMError, Provider, Reply, TransientError
from usage import CLAUDE, Usage

log = logging.getLogger(__name__)

_JSON_OUTPUT = {"type": "json_schema", "schema": PARSED_MESSAGE_SCHEMA}


class ClaudeProvider(Provider):
    name = CLAUDE
    supports_images = True

    def __init__(self) -> None:
        self._client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

    async def aclose(self) -> None:
        await self._client.close()

    # --- публичные операции -------------------------------------------------

    async def complete(self, *, system: str, user: str, kind: str) -> Reply:
        response = await self._call(
            model=config.CLAUDE_MODEL,
            max_tokens=4096,
            system=system,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": user}],
        )
        if response.stop_reason == "refusal":
            raise LLMError("Модель отказалась отвечать на этот вопрос.")
        return Reply(self._text_of(response), self._usage(response, kind))

    async def complete_json(self, *, system: str, user: str, kind: str) -> Reply:
        response = await self._call(
            model=config.CLAUDE_PARSER_MODEL,
            max_tokens=4096,
            system=system,
            output_config={"format": _JSON_OUTPUT, "effort": "low"},
            messages=[{"role": "user", "content": user}],
        )
        return Reply(self._json_text(response), self._usage(response, kind))

    async def read_image(
        self, *, system: str, user: str, image: bytes, media_type: str, kind: str
    ) -> Reply:
        """
        В отличие от разбора текста здесь включено adaptive thinking: мелкий
        шрифт, скидки и весовые позиции требуют аккуратности, а чеки приходят
        редко — экономить на них смысла нет.
        """
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
            {"type": "text", "text": user},
        ]
        response = await self._call(
            model=config.CLAUDE_VISION_MODEL,
            max_tokens=8192,
            system=system,
            thinking={"type": "adaptive"},
            output_config={"format": _JSON_OUTPUT, "effort": "medium"},
            messages=[{"role": "user", "content": content}],
        )
        if response.stop_reason == "max_tokens":
            log.warning("Чек не поместился в ответ целиком")
        return Reply(self._json_text(response), self._usage(response, kind))

    # --- внутреннее ---------------------------------------------------------

    async def _call(self, **kwargs) -> anthropic.types.Message:
        try:
            return await self._client.messages.create(**kwargs)
        except anthropic.RateLimitError as exc:
            raise TransientError(
                "Слишком много запросов к Claude, попробуйте через минуту."
            ) from exc
        except anthropic.APIStatusError as exc:
            if fatal := self._fatal(exc):
                raise fatal from exc
            log.exception("Claude вернул ошибку %s", exc.status_code)
            raise TransientError("Claude сейчас недоступен, попробуйте позже.") from exc
        except anthropic.APIConnectionError as exc:
            raise TransientError(
                "Не получилось достучаться до Claude — проверьте интернет."
            ) from exc

    @staticmethod
    def _fatal(exc: anthropic.APIStatusError) -> LLMError | None:
        """
        Отличить «сломана настройка» от «временно не повезло».

        Первое надо показать в чате, иначе бот молча перестаёт записывать покупки
        и выглядит просто сломанным. Второе — залогировать и забыть.
        """
        # Текст ошибки лежит то в .message, то в теле ответа — смотрим везде.
        text = f"{getattr(exc, 'message', '')} {getattr(exc, 'body', '')}".lower()
        kind = getattr(exc, "type", "") or ""

        if "credit balance" in text or "billing" in text or kind == "billing_error":
            return LLMError(
                "На балансе Anthropic закончились деньги, я не могу обратиться к Claude.\n"
                "Пополните счёт: console.anthropic.com → Plans &amp; Billing."
            )
        if isinstance(exc, anthropic.AuthenticationError):
            return LLMError("Claude не принял ключ — проверьте ANTHROPIC_API_KEY в .env.")
        if isinstance(exc, anthropic.PermissionDeniedError):
            return LLMError("У ключа нет доступа к этой модели — проверьте .env.")
        if isinstance(exc, anthropic.NotFoundError):
            return LLMError(
                "Такой модели нет — проверьте CLAUDE_MODEL / CLAUDE_PARSER_MODEL / "
                "CLAUDE_VISION_MODEL в .env."
            )
        return None

    @staticmethod
    def _text_of(response: anthropic.types.Message) -> str:
        """Собрать текстовые блоки ответа (пропуская thinking и прочее)."""
        return "".join(b.text for b in response.content if b.type == "text").strip()

    @classmethod
    def _json_text(cls, response: anthropic.types.Message) -> str:
        if response.stop_reason == "refusal":
            log.warning("Модель отказалась разбирать сообщение")
            return ""
        return cls._text_of(response)

    @staticmethod
    def _usage(response: anthropic.types.Message, kind: str) -> Usage:
        """
        Модель берём из ответа, а не из конфига: так в логе окажется то, что
        реально отработало. Поля кеша появляются не всегда — отсюда getattr.
        """
        stats = response.usage
        return Usage(
            kind=kind,
            provider=CLAUDE,
            model=getattr(response, "model", "unknown"),
            input_tokens=getattr(stats, "input_tokens", 0) or 0,
            output_tokens=getattr(stats, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(stats, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(stats, "cache_creation_input_tokens", 0) or 0,
        )
