"""
llm.py — всё общение бота с YandexGPT (Yandex Cloud Foundation Models).

Пять задач:
  1. parse_message   — разобрать сообщение из чата в структурированные покупки;
  2. parse_receipt   — прочитать фотографию чека (Vision OCR + разбор текста);
  3. ask             — ответить на свободный вопрос по данным о покупках;
  4. suggest_recipes — предложить блюда из того, что лежит в холодильнике;
  5. analyze_week    — короткий анализ трат за неделю для дайджеста.

Текст: POST /foundationModels/v1/completion. Структурированный ответ задаётся
полем json_schema — модель обязана вернуть валидный JSON по схеме из models.py.

Чеки: текстовая модель картинок не видит, поэтому фото сначала уходит в
Yandex Vision OCR, а модели достаётся распознанный текст. Отсюда два вызова
на один чек и, соответственно, две записи о расходе.
"""
from __future__ import annotations

import base64
import logging

import httpx

import config
import prompts
from models import PARSED_MESSAGE_SCHEMA, ParsedMessage
from usage import (
    KIND_ANALYSIS,
    KIND_ASK,
    KIND_PARSE,
    KIND_RECEIPT,
    KIND_RECIPE,
    Usage,
)

log = logging.getLogger(__name__)

COMPLETION_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
OCR_URL = "https://ocr.api.cloud.yandex.net/ocr/v1/recognizeText"

# Yandex Vision понимает не любой mime — приводим к тому, что он ждёт.
_OCR_MIME = {"image/jpeg": "JPEG", "image/png": "PNG", "application/pdf": "PDF"}

# Пустой результат — возвращаем его при любой ошибке разбора, чтобы бот не падал в чате.
_NOT_A_PURCHASE = ParsedMessage(
    is_purchase=False, store=None, bought_on=None, items=[], total=None, note=None
)

_client: httpx.AsyncClient | None = None


class LLMError(RuntimeError):
    """Ошибка, которую должен увидеть человек: чинится настройками или деньгами."""


class TransientError(LLMError):
    """
    Временно не повезло: сеть, лимит запросов, 5xx у Яндекса.

    Отличается тем, что при фоновом разборе покупок бот про неё молчит: ругаться
    на каждый сетевой чих в чате, где люди просто пишут о покупках, — худшее из
    зол. Там, где человек ждёт ответа (вопрос, рецепт, чек), она всё равно покажется.
    """


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=config.LLM_TIMEOUT,
            headers={
                "Authorization": f"Api-Key {config.YANDEX_API_KEY}",
                "x-folder-id": config.YANDEX_FOLDER_ID,
            },
        )
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# --- операции ---------------------------------------------------------------

async def parse_message(text: str, today: str) -> tuple[ParsedMessage, list[Usage]]:
    """
    Разобрать сообщение чата.

    Это фоновая работа: человек просто написал в чат, ответа не ждёт. Поэтому
    временные сбои проглатываем (вернём is_purchase=False), а вот ошибки
    настройки бросаем наружу — про них пользователь должен узнать сразу, иначе
    бот молча перестанет записывать покупки.
    """
    try:
        text_out, spent = await _completion(
            model=config.YANDEX_PARSER_MODEL,
            system=prompts.PARSER_SYSTEM,
            user=f"Сегодня {today}.\n\nСообщение из чата:\n{text}",
            kind=KIND_PARSE,
            max_tokens=4096,
            schema=PARSED_MESSAGE_SCHEMA,
        )
    except TransientError as exc:
        log.warning("Разбор сообщения не состоялся: %s", exc)
        return _NOT_A_PURCHASE, []
    except LLMError:
        raise
    except Exception:  # noqa: BLE001 — неожиданный сбой не должен ронять бота
        log.exception("Не удалось разобрать сообщение")
        return _NOT_A_PURCHASE, []

    return _to_parsed(text_out), [spent]


async def parse_receipt(
    image: bytes,
    media_type: str,
    caption: str | None,
    today: str,
) -> tuple[ParsedMessage, list[Usage]]:
    """
    Прочитать фотографию чека: распознать текст через OCR, затем разобрать его.
    """
    text, ocr_spent = await _ocr(image, media_type)
    log.info("OCR распознал %s символов", len(text))

    hint = f"\n\nПодпись к фото от отправителя: {caption}" if caption else ""
    task = (
        f"Сегодня {today}. Разбери этот чек.{hint}\n\n"
        "Если в подписи названа общая сумма, используй её только для проверки: "
        "источник истины — позиции самого чека.\n\n"
        f"Распознанный текст чека:\n{text}"
    )
    text_out, spent = await _completion(
        model=config.YANDEX_VISION_MODEL,
        system=prompts.RECEIPT_TEXT_SYSTEM,
        user=task,
        kind=KIND_RECEIPT,
        max_tokens=8192,
        schema=PARSED_MESSAGE_SCHEMA,
    )
    return _to_parsed(text_out), [ocr_spent, spent]


async def ask(question: str, context: str) -> tuple[str, list[Usage]]:
    """Свободный вопрос по данным о покупках."""
    text, spent = await _completion(
        model=config.YANDEX_MODEL,
        system=prompts.ASSISTANT_SYSTEM,
        user=f"Данные о покупках семьи:\n\n{context}\n\n---\n\nВопрос: {question}",
        kind=KIND_ASK,
        max_tokens=4096,
    )
    return (text or "Мне нечего добавить по этим данным."), [spent]


async def suggest_recipes(fridge_text: str, wish: str | None) -> tuple[str, list[Usage]]:
    """Что приготовить из имеющегося."""
    wish_line = f"\n\nПожелание: {wish}" if wish else ""
    text, spent = await _completion(
        model=config.YANDEX_MODEL,
        system=prompts.RECIPE_SYSTEM,
        user=f"Продукты, купленные недавно:\n\n{fridge_text}{wish_line}",
        kind=KIND_RECIPE,
        max_tokens=4096,
    )
    return (text or "Из этого набора ничего не придумалось."), [spent]


async def analyze_week(stats_text: str) -> tuple[str, list[Usage]]:
    """Короткий разбор трат за неделю для дайджеста."""
    text, spent = await _completion(
        model=config.YANDEX_MODEL,
        system=prompts.WEEKLY_SYSTEM,
        user=stats_text,
        kind=KIND_ANALYSIS,
        max_tokens=2048,
    )
    return text, [spent]


# --- HTTP -------------------------------------------------------------------

async def _completion(
    *,
    model: str,
    system: str,
    user: str,
    kind: str,
    max_tokens: int,
    schema: dict | None = None,
) -> tuple[str, Usage]:
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

    data = await _post(COMPLETION_URL, payload)
    result = data.get("result") or {}

    alternatives = result.get("alternatives") or []
    if not alternatives:
        raise LLMError("YandexGPT вернул пустой ответ.")
    alternative = alternatives[0]
    if alternative.get("status") == "ALTERNATIVE_STATUS_CONTENT_FILTER":
        raise LLMError("YandexGPT отказался отвечать на этот запрос.")

    text = (alternative.get("message", {}).get("text") or "").strip()
    if schema is not None:
        text = _strip_json(text)

    stats = result.get("usage") or {}
    spent = Usage(
        kind=kind,
        model=model,
        input_tokens=_int(stats.get("inputTextTokens")),
        output_tokens=_int(stats.get("completionTokens")),
    )
    return text, spent


async def _ocr(image: bytes, media_type: str) -> tuple[str, Usage]:
    mime = _OCR_MIME.get(media_type)
    if mime is None:
        raise LLMError(f"Yandex Vision не принимает {media_type} — пришлите фото JPEG или PNG.")

    payload = {
        "mimeType": mime,
        "languageCodes": ["ru", "en"],
        "model": config.YANDEX_OCR_MODEL,
        "content": base64.standard_b64encode(image).decode("ascii"),
    }
    # Явно запрещаем Яндексу сохранять чеки для своих нужд.
    data = await _post(OCR_URL, payload, headers={"x-data-logging-enabled": "false"})

    text = (data.get("result", {}).get("textAnnotation", {}).get("fullText") or "").strip()
    if not text:
        raise LLMError("Не разобрал на фото ни строчки текста. Попробуйте снять чек поближе.")

    # OCR тарифицируется за страницу, а не за токены.
    spent = Usage(
        kind=KIND_RECEIPT,
        model=f"vision-ocr/{config.YANDEX_OCR_MODEL}",
        cost_override=config.YANDEX_OCR_PRICE_PER_PAGE,
    )
    return text, spent


async def _post(url: str, payload: dict, headers: dict | None = None) -> dict:
    try:
        response = await _http().post(url, json=payload, headers=headers)
    except httpx.RequestError as exc:
        raise TransientError(
            "Не получилось достучаться до Yandex Cloud — проверьте интернет."
        ) from exc

    if response.is_success:
        return response.json()
    raise _http_error(response)


# --- вспомогательное --------------------------------------------------------

def _to_parsed(text: str) -> ParsedMessage:
    """Ответ модели -> ParsedMessage. При любой неожиданности — «это не покупка»."""
    if not text:
        return _NOT_A_PURCHASE
    try:
        return ParsedMessage.model_validate_json(text)
    except ValueError:
        log.exception("Не разобрался ответ модели: %.300s", text)
        return _NOT_A_PURCHASE


def _strip_json(text: str) -> str:
    """
    Выковырять JSON из ответа модели.

    Обычно YandexGPT с json_schema возвращает чистый JSON, но иногда добавляет
    ```-обёртку или предисловие — на этот случай подстрахуемся.
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text
        text = text.rsplit("```", 1)[0].strip()

    start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
    if start == -1:
        return text
    end = max(text.rfind("}"), text.rfind("]"))
    return text[start : end + 1] if end > start else text[start:]


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
            "в .env, а также права сервисного аккаунта (ai.languageModels.user, "
            "для чеков ещё ai.vision.user)."
        )
    if response.status_code == 404:
        return LLMError("Такой модели нет — проверьте YANDEX_MODEL в .env.")
    if response.status_code == 402:
        return LLMError("На платёжном аккаунте Yandex Cloud закончились деньги.")
    if response.status_code == 429:
        return TransientError("Слишком много запросов к YandexGPT, попробуйте через минуту.")
    return TransientError("YandexGPT сейчас недоступен, попробуйте позже.")
