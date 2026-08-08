"""
api/auth.py — кто спрашивает и имеет ли он право.

API висит в открытом интернете: адрес мини-приложения знает любой, кто видел
ссылку. Поэтому каждый запрос проходит две независимые проверки.

1. Кто пользователь. Telegram при открытии страницы кладёт в
   `window.Telegram.WebApp.initData` строку с данными пользователя и подписью
   HMAC-SHA256 на секрете, выведенном из токена бота. Проверить подпись может
   только тот, у кого есть токен, — то есть наш сервер. Подделать `user.id`,
   не зная токена, нельзя. Саму проверку делает aiogram
   (`safe_parse_webapp_init_data`), мы добавляем срок годности подписи.

2. Что ему можно. Подпись говорит «это Андрей», но не говорит «Андрею можно
   смотреть траты чата -100123». Это отдельный вопрос, и отвечает на него сам
   Telegram: бот спрашивает getChatMember и смотрит, состоит ли пользователь в
   группе. Поэтому подставить чужой chat_id в ссылку бесполезно.

Токен бота при этом никогда не покидает сервер.
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.utils.web_app import WebAppInitData, safe_parse_webapp_init_data

import config
import retry

log = logging.getLogger(__name__)

# Статусы участника, при которых показываем данные чата. 'left' и 'kicked'
# getChatMember тоже возвращает — бывшего участника внутрь пускать не надо.
MEMBER_STATUSES = frozenset({"creator", "administrator", "member", "restricted"})

# Ответ getChatMember кешируем: приложение делает несколько запросов подряд,
# и дёргать Bot API на каждый — лишняя задержка и лишний расход лимитов.
MEMBERSHIP_TTL = 300.0
_membership: dict[tuple[int, int], tuple[float, bool]] = {}

# Длина подписи в ссылке-приглашении, символов base64url.
_SIG_LEN = 16


class AuthError(Exception):
    """Запрос не прошёл проверку. Текст уходит клиенту как есть."""


class UpstreamError(Exception):
    """
    Telegram не ответил, и прав мы не знаем.

    Отдельный тип нужен, чтобы не выдавать обрыв связи за отказ в доступе. Разница
    для человека принципиальная: «нет прав» — это тупик, а «связь подвела» — повод
    нажать «попробовать снова». Клиенту уходит 503, а не 403.
    """


# --- ссылка на чат ----------------------------------------------------------
#
# Открыть мини-приложение из группы можно только прямой ссылкой
# t.me/<бот>/<приложение>?startapp=<параметр>: inline-кнопки с web_app Telegram
# разрешает исключительно в личке. Значение startapp приходит на страницу как
# start_param внутри подписанных данных, так что в нём и едет id чата.
#
# Алфавит startapp ограничен: A-Z, a-z, 0-9, '_' и '-'. base64url в него
# укладывается. Подпись отрезает возможность перебирать чужие chat_id: без неё
# запрос всё равно упёрся бы в getChatMember, но пусть не доходит и до него.


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _sign(payload: str) -> str:
    digest = hmac.new(config.TELEGRAM_BOT_TOKEN.encode(), payload.encode(), hashlib.sha256).digest()
    return _b64(digest)[:_SIG_LEN]


def pack_chat(chat_id: int) -> str:
    """id чата -> значение для ?startapp=."""
    payload = _b64(str(chat_id).encode())
    return payload + _sign(payload)


def unpack_chat(token: str) -> int:
    """Значение start_param -> id чата. Бросает AuthError, если подпись не сошлась."""
    payload, signature = token[:-_SIG_LEN], token[-_SIG_LEN:]
    if not payload or not hmac.compare_digest(signature, _sign(payload)):
        raise AuthError("ссылка повреждена или устарела")
    try:
        padding = "=" * (-len(payload) % 4)
        return int(base64.urlsafe_b64decode(payload + padding).decode())
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise AuthError("ссылка повреждена или устарела") from exc


# --- подпись Telegram -------------------------------------------------------


def parse_init_data(raw: str) -> WebAppInitData:
    """Проверить подпись и срок годности initData. -> разобранные данные."""
    if not raw:
        raise AuthError("нет данных авторизации — откройте приложение из Telegram")

    try:
        data = safe_parse_webapp_init_data(config.TELEGRAM_BOT_TOKEN, raw)
    except ValueError as exc:
        # Сюда же попадает несовпавшая подпись. Что именно не так — клиенту
        # знать незачем, а в лог не пишем саму строку: в ней данные пользователя.
        log.warning("initData не прошла проверку подписи")
        raise AuthError("подпись не подтвердилась — переоткройте приложение") from exc

    issued = data.auth_date
    if issued.tzinfo is None:
        issued = issued.replace(tzinfo=dt.timezone.utc)
    age = dt.datetime.now(dt.timezone.utc) - issued
    if age.total_seconds() > config.WEBAPP_AUTH_TTL:
        raise AuthError("сессия истекла — переоткройте приложение")

    if data.user is None:
        raise AuthError("Telegram не передал пользователя")

    return data


# --- права на чат -----------------------------------------------------------


async def is_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    """
    Состоит ли пользователь в чате. Ответ кешируется на MEMBERSHIP_TTL секунд.

    Кешируется только ответ Telegram. Если Telegram не ответил, бросаем
    UpstreamError: запомнить обрыв связи как «доступа нет» значило бы закрыть
    человеку приложение на пять минут из-за секундной сетевой заминки — ровно это
    и происходило, пока обе ситуации обрабатывались одинаково.
    """
    key = (chat_id, user_id)
    cached = _membership.get(key)
    now = time.monotonic()
    if cached is not None and now < cached[0]:
        return cached[1]

    try:
        member = await retry.call(
            lambda: bot.get_chat_member(chat_id, user_id,
                                        request_timeout=int(config.TELEGRAM_TIMEOUT)),
            what=f"getChatMember({chat_id}, {user_id})",
        )
    except retry.TRANSIENT as exc:
        raise UpstreamError("Telegram сейчас не отвечает — попробуйте ещё раз") from exc
    except TelegramAPIError as exc:
        # А это уже ответ Telegram, просто отрицательный: чата нет, бота из него
        # выгнали, id чужой. Доступа нет, и запомнить это можно.
        log.info("getChatMember(%s, %s) отказано: %s", chat_id, user_id, exc)
        _membership[key] = (now + MEMBERSHIP_TTL, False)
        return False

    allowed = member.status in MEMBER_STATUSES
    _membership[key] = (now + MEMBERSHIP_TTL, allowed)
    return allowed


@dataclass(slots=True, frozen=True)
class Access:
    """Проверенный запрос: кто спрашивает и про какой чат."""

    user_id: int
    user_name: str
    chat_id: int


async def authorize(bot: Bot, init_data_raw: str, chat_param: str | None) -> Access:
    """
    Полная проверка запроса от мини-приложения.

    chat_param — либо значение start_param из ссылки, либо id чата строкой:
    приложение переключает чаты уже после открытия, и ссылку при этом не
    перевыпустить. Оба пути одинаково упираются в проверку членства, поэтому
    принимать «сырой» id безопасно.
    """
    data = parse_init_data(init_data_raw)
    user = data.user
    chat_id = resolve_chat(chat_param or data.start_param)

    if config.ALLOWED_CHAT_IDS and chat_id not in config.ALLOWED_CHAT_IDS:
        raise AuthError("этот чат не в белом списке бота")
    if not await is_member(bot, chat_id, user.id):
        raise AuthError("вы не состоите в этом чате")

    return Access(user_id=user.id, user_name=user.first_name, chat_id=chat_id)


def resolve_chat(value: str | None) -> int:
    """Значение из ссылки или запроса -> id чата."""
    value = (value or "").strip()
    if not value:
        raise AuthError("не понятно, чей бюджет показывать — откройте приложение из чата")
    if value.lstrip("-").isdigit():
        return int(value)
    return unpack_chat(value)
