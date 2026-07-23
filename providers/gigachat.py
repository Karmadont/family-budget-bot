"""
providers/gigachat.py — GigaChat (Сбер).

Два отличия от остальных провайдеров, из-за которых кода здесь больше:

1. Авторизация двухступенчатая. Ключ из личного кабинета меняется на access
   token со сроком жизни ~30 минут — храним его и обновляем по истечении.
2. Строгой схемы ответа, как output_config у Claude, здесь нет. Ближайшее —
   вызов функции: описываем схему как параметры функции и просим модель её
   «вызвать». Если модель всё же ответит текстом, разбираем текст.

TLS: сертификат GigaChat подписан НУЦ Минцифры. Без его корневого сертификата
в системе httpx откажется соединяться — путь к нему задаётся в GIGACHAT_CA_BUNDLE.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

import httpx

import config
from models import PARSED_MESSAGE_SCHEMA_FLAT
from providers.base import LLMError, Provider, Reply, TransientError, strip_json
from usage import GIGACHAT, Usage

log = logging.getLogger(__name__)

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
BASE_URL = "https://gigachat.devices.sberbank.ru/api/v1"

# Имя функции, через которую вытягиваем структурированный ответ.
_FUNCTION_NAME = "save_purchase"


class GigaChatProvider(Provider):
    name = GIGACHAT
    supports_images = True

    def __init__(self) -> None:
        if not config.GIGACHAT_VERIFY_SSL:
            log.warning(
                "GIGACHAT_VERIFY_SSL=false — TLS-сертификат GigaChat не проверяется. "
                "Лучше положить сертификат НУЦ Минцифры и указать GIGACHAT_CA_BUNDLE."
            )
        verify: bool | str = False if not config.GIGACHAT_VERIFY_SSL else (
            config.GIGACHAT_CA_BUNDLE or True
        )
        self._client = httpx.AsyncClient(timeout=config.LLM_TIMEOUT, verify=verify)
        self._token: str = ""
        self._token_until: float = 0.0
        self._token_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- публичные операции -------------------------------------------------

    async def complete(self, *, system: str, user: str, kind: str) -> Reply:
        return await self._chat(
            model=config.GIGACHAT_MODEL,
            messages=_messages(system, user),
            kind=kind,
            max_tokens=4096,
        )

    async def complete_json(self, *, system: str, user: str, kind: str) -> Reply:
        return await self._chat(
            model=config.GIGACHAT_PARSER_MODEL,
            messages=_messages(system, user),
            kind=kind,
            max_tokens=4096,
            as_json=True,
        )

    async def read_image(
        self, *, system: str, user: str, image: bytes, media_type: str, kind: str
    ) -> Reply:
        file_id = await self._upload(image, media_type)
        messages = _messages(system, user)
        messages[-1]["attachments"] = [file_id]
        return await self._chat(
            model=config.GIGACHAT_VISION_MODEL,
            messages=messages,
            kind=kind,
            max_tokens=8192,
            as_json=True,
        )

    # --- авторизация --------------------------------------------------------

    async def _access_token(self) -> str:
        # Токен живёт около получаса. Обновляем с запасом в минуту, под замком —
        # иначе на всплеске сообщений полетит несколько запросов сразу.
        async with self._token_lock:
            if self._token and time.time() < self._token_until - 60:
                return self._token

            try:
                response = await self._client.post(
                    OAUTH_URL,
                    headers={
                        "Authorization": f"Basic {config.GIGACHAT_AUTH_KEY}",
                        "RqUID": str(uuid.uuid4()),
                        "Accept": "application/json",
                    },
                    data={"scope": config.GIGACHAT_SCOPE},
                )
            except httpx.RequestError as exc:
                raise _connection_error(exc) from exc

            if not response.is_success:
                log.error("GigaChat не выдал токен (%s): %s", response.status_code, response.text[:500])
                if response.status_code in (400, 401, 403):
                    raise LLMError(
                        "GigaChat не принял ключ — проверьте GIGACHAT_AUTH_KEY и "
                        "GIGACHAT_SCOPE в .env."
                    )
                raise TransientError("GigaChat не выдал токен доступа, попробуйте позже.")

            data = response.json()
            self._token = data.get("access_token", "")
            if not self._token:
                raise LLMError("GigaChat вернул ответ без токена доступа.")
            # expires_at приходит в миллисекундах.
            self._token_until = float(data.get("expires_at", 0)) / 1000 or time.time() + 1500
            return self._token

    # --- внутреннее ---------------------------------------------------------

    async def _chat(
        self,
        *,
        model: str,
        messages: list[dict],
        kind: str,
        max_tokens: int,
        as_json: bool = False,
    ) -> Reply:
        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if as_json:
            payload["functions"] = [
                {
                    "name": _FUNCTION_NAME,
                    "description": "Сохранить разобранные покупки.",
                    "parameters": PARSED_MESSAGE_SCHEMA_FLAT,
                }
            ]
            payload["function_call"] = {"name": _FUNCTION_NAME}

        data = await self._post(f"{BASE_URL}/chat/completions", payload)

        choices = data.get("choices") or []
        if not choices:
            raise LLMError("GigaChat вернул пустой ответ.")
        message = choices[0].get("message") or {}

        text = _content_of(message, as_json=as_json)
        stats = data.get("usage") or {}
        spent = Usage(
            kind=kind,
            provider=GIGACHAT,
            model=data.get("model") or model,
            input_tokens=int(stats.get("prompt_tokens") or 0),
            output_tokens=int(stats.get("completion_tokens") or 0),
        )
        return Reply(text, spent)

    async def _upload(self, image: bytes, media_type: str) -> str:
        """Залить картинку в хранилище GigaChat и получить её id."""
        suffix = "png" if media_type == "image/png" else "jpg"
        try:
            response = await self._client.post(
                f"{BASE_URL}/files",
                headers=await self._auth_headers(),
                files={"file": (f"receipt.{suffix}", image, media_type)},
                data={"purpose": "general"},
            )
        except httpx.RequestError as exc:
            raise _connection_error(exc) from exc

        if not response.is_success:
            log.error("GigaChat не принял файл (%s): %s", response.status_code, response.text[:500])
            raise LLMError("GigaChat не принял фотографию чека.")

        file_id = response.json().get("id")
        if not file_id:
            raise LLMError("GigaChat не вернул идентификатор загруженного файла.")
        return file_id

    async def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {await self._access_token()}",
            "X-Request-ID": str(uuid.uuid4()),
        }

    async def _post(self, url: str, payload: dict) -> dict:
        headers = await self._auth_headers()
        try:
            response = await self._client.post(url, json=payload, headers=headers)
            if response.status_code == 401:
                # Токен протух раньше срока — выбрасываем и пробуем ещё раз.
                self._token_until = 0.0
                response = await self._client.post(
                    url, json=payload, headers=await self._auth_headers()
                )
        except httpx.RequestError as exc:
            raise _connection_error(exc) from exc

        if response.is_success:
            return response.json()
        raise _http_error(response)


def _messages(system: str, user: str) -> list[dict]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _content_of(message: dict, *, as_json: bool) -> str:
    """Достать полезную часть ответа: аргументы функции либо обычный текст."""
    if as_json:
        call = message.get("function_call") or {}
        arguments = call.get("arguments")
        if isinstance(arguments, dict):
            return json.dumps(arguments, ensure_ascii=False)
        if isinstance(arguments, str) and arguments.strip():
            return strip_json(arguments)

    text = (message.get("content") or "").strip()
    return strip_json(text) if as_json else text


def _connection_error(exc: httpx.RequestError) -> LLMError:
    # Сертификат — это настройка, а не «не повезло»: про него говорим всегда.
    if isinstance(exc, httpx.ConnectError) and "certificate" in str(exc).lower():
        return LLMError(
            "TLS-сертификат GigaChat не проверился. Установите корневой сертификат "
            "НУЦ Минцифры и укажите путь к нему в GIGACHAT_CA_BUNDLE."
        )
    return TransientError("Не получилось достучаться до GigaChat — проверьте интернет.")


def _http_error(response: httpx.Response) -> LLMError:
    body = response.text[:500]
    log.error("GigaChat вернул %s: %s", response.status_code, body)

    if response.status_code in (401, 403):
        return LLMError("GigaChat не принял токен — проверьте GIGACHAT_AUTH_KEY и GIGACHAT_SCOPE.")
    if response.status_code == 404:
        return LLMError("Такой модели нет — проверьте GIGACHAT_MODEL в .env.")
    if response.status_code == 402:
        return LLMError("На балансе GigaChat закончились токены — пополните пакет.")
    if response.status_code == 413:
        return LLMError("Запрос к GigaChat слишком большой.")
    if response.status_code == 429:
        return TransientError("Слишком много запросов к GigaChat, попробуйте через минуту.")
    return TransientError("GigaChat сейчас недоступен, попробуйте позже.")
