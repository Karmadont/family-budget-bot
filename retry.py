"""
retry.py — повторы для вызовов Telegram.

Зачем. Связь до api.telegram.org рвётся: запрос не отвергается, а зависает и
отваливается по таймауту. Один такой обрыв стоил пользователю подтверждения уже
записанной покупки — деньги учтены, а в чате тишина.

Что повторяем:
  * TelegramNetworkError — связь не дошла;
  * TelegramServerError — 5xx, у Telegram проблемы;
  * TelegramRetryAfter — попросили подождать, ждём ровно столько.

Что не повторяем: 400 и 403. Это ответы «так нельзя» — от повтора они не
изменятся, а время потратят.

Про безопасность повторов. Если Telegram успел принять отправку сообщения, но
ответ до нас не дошёл, повтор создаст второе сообщение. Для подтверждения
покупки это приемлемо: два «✅ Записал» — мелкая неприятность, ноль «✅ Записал»
заставляет человека писать всё заново. Для действий, которые нельзя выполнить
дважды, этим помощником пользоваться нельзя.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.methods import TelegramMethod

import config

log = logging.getLogger(__name__)

T = TypeVar("T")

# Сбои, которые проходят сами.
TRANSIENT = (TelegramNetworkError, TelegramServerError)

# Пауза перед повтором: 1 с, 2 с, 4 с… Растёт, чтобы не долбиться в стену,
# когда связи нет совсем.
BASE_DELAY = 1.0
MAX_DELAY = 8.0


async def call(
    make_request: Callable[[], Awaitable[T]],
    *,
    what: str,
    attempts: int | None = None,
) -> T:
    """
    Выполнить запрос, повторяя его при обрыве связи.

    make_request — фабрика, а не готовая корутина: корутину нельзя ждать дважды,
    и на второй попытке она бросила бы RuntimeError вместо запроса.

    Последняя неудача пробрасывается наружу: решать, что делать с окончательно
    несработавшим вызовом, должен тот, кто его заказывал.
    """
    total = attempts if attempts is not None else config.TELEGRAM_RETRIES
    delay = BASE_DELAY

    for attempt in range(1, total + 1):
        try:
            return await make_request()
        except TelegramRetryAfter as exc:
            # Telegram сам сказал, сколько ждать. Спорить бессмысленно.
            if attempt == total:
                raise
            log.warning("%s: Telegram просит подождать %s с", what, exc.retry_after)
            await asyncio.sleep(exc.retry_after)
        except TRANSIENT as exc:
            if attempt == total:
                log.warning("%s: не удалось за %s попыт. — %s", what, total, exc)
                raise
            log.info("%s: попытка %s из %s не прошла (%s), жду %.0f с",
                     what, attempt, total, exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_DELAY)

    raise AssertionError("недостижимо: цикл всегда завершается return или raise")


async def send(bot: Bot, method: TelegramMethod[T], *, what: str,
               attempts: int | None = None) -> T:
    """
    Отправить готовый метод aiogram с коротким таймаутом и повторами.

    Метод берём несделанным: `message.reply(text)` возвращает объект SendMessage,
    а не отправляет его, — поэтому один и тот же объект можно послать повторно.
    """
    return await call(
        lambda: bot(method, request_timeout=int(config.TELEGRAM_TIMEOUT)),
        what=what,
        attempts=attempts,
    )
