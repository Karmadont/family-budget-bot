"""
providers/yandex.py — YandexGPT (Yandex Cloud Foundation Models).

Текст: POST /foundationModels/v1/completion. Структурированный ответ задаётся
полем json_schema — как и у Claude, модель обязана вернуть валидный JSON.

Чеки: текстовая модель картинок не видит, поэтому фото сначала уходит в
Yandex Vision OCR, а модели достаётся распознанный текст. Отсюда два вызова
на один чек и, соответственно, две записи о расходе.
"""
from __future__ import annotations

import base64
import logging

import httpx

import config
from models import PARSED_MESSAGE_SCHEMA_FLAT
from providers.base import LLMError, Provider, Reply, TransientError, strip_json
from usage import KIND_RECEIPT, RUB, Usage, YANDEXGPT

log = logging.getLogger(__name__)

COMPLETION_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
OCR_URL = "https://ocr.api.cloud.yandex.net/ocr/v1/recognizeText"

# Yandex Vision понимает не любой mime — приводим к тому, что он ждёт.
_OCR_MIME = {"image/jpeg": "JPEG", "image/png": "PNG", "application/pdf": "PDF"}


class YandexProvider(Provider):
    name = YANDEXGPT
    supports_ocr = True

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=config.LLM_TIMEOUT,
            headers={
                "Authorization": f"Api-Key {config.YANDEX_API_KEY}",
                "x-folder-id": config.YANDEX_FOLDER_ID,
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- публичные операции -------------------------------------------------

    async def complete(self, *, system: str, user: str, kind: str) -> Reply:
        return await self._completion(
            model=config.YANDEX_MODEL,
            system=system,
            user=user,
            kind=kind,
            max_tokens=4096,
        )

    async def complete_json(self, *, system: str, user: str, kind: str) -> Reply:
        return await self._completion(
            model=config.YANDEX_PARSER_MODEL,
            system=system,
            user=user,
            kind=kind,
            max_tokens=4096,
            schema=PARSED_MESSAGE_SCHEMA_FLAT,
        )

    async def read_text(self, *, system: str, user: str, kind: str) -> Reply:
        """Разбор распознанного чека — отдельной моделью из YANDEX_VISION_MODEL."""
        return await self._completion(
            model=config.YANDEX_VISION_MODEL,
            system=system,
            user=user,
            kind=kind,
            max_tokens=8192,
            schema=PARSED_MESSAGE_SCHEMA_FLAT,
        )

    async def ocr(self, image: bytes, media_type: str) -> tuple[str, Usage]:
        mime = _OCR_MIME.get(media_type)
        if mime is None:
            raise LLMError(f"Yandex Vision не принимает {media_type} — пришлите фото JPEG или PNG.")

        payload = {
            "mimeType": mime,
            "languageCodes": ["ru", "en"],
            "model": config.YANDEX_OCR_MODEL,
            "content": base64.standard_b64encode(image).decode("ascii"),
        }
        data = await self._post(
            OCR_URL,
            payload,
            # Явно запрещаем Яндексу сохранять чеки для своих нужд.
            headers={"x-data-logging-enabled": "false"},
        )

        text = (data.get("result", {}).get("textAnnotation", {}).get("fullText") or "").strip()
        if not text:
            raise LLMError("Не разобрал на фото ни строчки текста. Попробуйте снять чек поближе.")

        # OCR тарифицируется за страницу, а не за токены.
        spent = Usage(
            kind=KIND_RECEIPT,
            provider=YANDEXGPT,
            model=f"vision-ocr/{config.YANDEX_OCR_MODEL}",
            cost_override=config.YANDEX_OCR_PRICE_PER_PAGE,
            currency_override=RUB,
        )
        return text, spent

    # --- внутреннее ---------------------------------------------------------

    async def _completion(
        self,
        *,
        model: str,
        system: str,
        user: str,
        kind: str,
        max_tokens: int,
        schema: dict | None = None,
    ) -> Reply:
        payload: dict = {
            "modelUri": f"gpt://{config.YANDEX_FOLDER_ID}/{model}",
            "completionOptions": {
                "stream": False,
                "temperature": 0.2,
                # Яндекс ждёт maxTokens строкой (в API это int64).
                "maxTokens": str(max_tokens),
            },
            "messages": [
                {"role": "system", "text": system},
                {"role": "user", "text": user},
            ],
        }
        if schema is not None:
            payload["json_schema"] = {"schema": schema}

        data = await self._post(COMPLETION_URL, payload)
        result = data.get("result") or {}

        alternatives = result.get("alternatives") or []
        if not alternatives:
            raise LLMError("YandexGPT вернул пустой ответ.")
        alternative = alternatives[0]
        if alternative.get("status") == "ALTERNATIVE_STATUS_CONTENT_FILTER":
            raise LLMError("YandexGPT отказался отвечать на этот запрос.")

        text = (alternative.get("message", {}).get("text") or "").strip()
        if schema is not None:
            text = strip_json(text)

        stats = result.get("usage") or {}
        spent = Usage(
            kind=kind,
            provider=YANDEXGPT,
            # modelVersion — это дата обучения, для прайса бесполезна: пишем модель.
            model=model,
            input_tokens=_int(stats.get("inputTextTokens")),
            output_tokens=_int(stats.get("completionTokens")),
        )
        return Reply(text, spent)

    async def _post(self, url: str, payload: dict, headers: dict | None = None) -> dict:
        try:
            response = await self._client.post(url, json=payload, headers=headers)
        except httpx.RequestError as exc:
            raise TransientError(
                "Не получилось достучаться до Yandex Cloud — проверьте интернет."
            ) from exc

        if response.is_success:
            return response.json()
        raise _http_error(response)


def _int(value) -> int:
    """Яндекс отдаёт счётчики токенов строками."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _http_error(response: httpx.Response) -> LLMError:
    """Ошибку настройки показываем в чате, остальное — общим текстом."""
    body = response.text[:500]
    log.error("Yandex Cloud вернул %s: %s", response.status_code, body)

    if response.status_code in (401, 403):
        return LLMError(
            "Yandex Cloud не принял ключ — проверьте YANDEX_API_KEY и YANDEX_FOLDER_ID "
            "в .env, а также права сервисного аккаунта (ai.languageModels.user)."
        )
    if response.status_code == 404:
        return LLMError("Такой модели нет — проверьте YANDEX_MODEL в .env.")
    if response.status_code == 402:
        return LLMError("На платёжном аккаунте Yandex Cloud закончились деньги.")
    if response.status_code == 429:
        return TransientError("Слишком много запросов к YandexGPT, попробуйте через минуту.")
    return TransientError("YandexGPT сейчас недоступен, попробуйте позже.")
