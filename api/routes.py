"""
api/routes.py — эндпоинты мини-приложения.

Каждый ответ — JSON с сырыми числами: форматированием занимается фронтенд, ему
всё равно нужно уметь это делать для анимаций и переключения периодов.

Единственная дверь внутрь — зависимость `access`: без проверенной подписи
Telegram и членства в чате ни один эндпоинт с данными не отработает.
"""
from __future__ import annotations

import collections
import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from fastapi import APIRouter, Depends, Header, Query, Request

import config
import db
from api import periods
from api.auth import Access, AuthError, authorize, is_member, pack_chat, parse_init_data

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

# Названия чатов меняются редко, а get_chat — сетевой вызов. Держим час.
_TITLE_TTL = 3600.0
_titles: dict[int, tuple[float, str]] = {}

TOP_LIMIT = 5


def _bot(request: Request) -> Bot:
    return request.app.state.bot


def _init_data(authorization: str = Header(default="")) -> str:
    """
    Достать initData из заголовка `Authorization: tma <initData>`.

    Схема `tma` — соглашение из документации Telegram. Класть подпись в query-строку
    нельзя: адреса оседают в логах прокси и в истории, а внутри подписи — данные
    пользователя.
    """
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "tma":
        raise AuthError("нет данных авторизации — откройте приложение из Telegram")
    return value.strip()


async def access(
    request: Request,
    chat: str | None = Query(default=None, description="start_param из ссылки или id чата"),
    init_data: str = Depends(_init_data),
) -> Access:
    """Проверенный доступ к данным конкретного чата."""
    return await authorize(_bot(request), init_data, chat)


async def chat_title(bot: Bot, chat_id: int) -> str:
    now = time.monotonic()
    cached = _titles.get(chat_id)
    if cached is not None and now < cached[0]:
        return cached[1]
    try:
        info = await bot.get_chat(chat_id)
        title = info.title or info.full_name or str(chat_id)
    except TelegramAPIError:
        title = "Чат"
    _titles[chat_id] = (now + _TITLE_TTL, title)
    return title


# --- кто я и куда мне можно ---------------------------------------------------


@router.get("/me")
async def me(request: Request, init_data: str = Depends(_init_data)) -> dict:
    """
    Пользователь и доступные ему чаты.

    Кандидаты — белый список, если он задан, иначе все чаты, где записана хоть
    одна покупка. Каждого кандидата проверяем через getChatMember: список чатов
    сам по себе утечка, и отдавать его без проверки нельзя.
    """
    bot = _bot(request)
    user = parse_init_data(init_data).user

    candidates = sorted(config.ALLOWED_CHAT_IDS) if config.ALLOWED_CHAT_IDS else await db.distinct_chats()
    chats = [
        {"id": chat_id, "title": await chat_title(bot, chat_id), "token": pack_chat(chat_id)}
        for chat_id in candidates
        if await is_member(bot, chat_id, user.id)
    ]

    return {
        "user": {"id": user.id, "name": user.first_name},
        "chats": chats,
        "currency": config.CURRENCY,
    }


# --- главный экран ------------------------------------------------------------


@router.get("/overview")
async def overview(
    request: Request,
    who: Access = Depends(access),
    period: str = Query(default=periods.DEFAULT_PERIOD),
) -> dict:
    """Всё, что нужно экрану «Обзор», одним запросом."""
    chat_id = who.chat_id
    earliest = await db.first_purchase_date(chat_id) if period == "all" else None
    span = periods.resolve(period, earliest)

    total, items, days = await db.period_summary(chat_id, span.since, span.until)
    categories = await db.category_stats(chat_id, span.since, span.until)
    top = await db.top_items(chat_id, span.since, span.until, TOP_LIMIT)
    daily = await db.daily_totals(chat_id, span.since, span.until)

    before = periods.previous(span)
    comparison = None
    if before is not None:
        was = await db.period_total(chat_id, before.since, before.until)
        comparison = {"total": was, "label": before.label,
                      "delta": total - was if was else None}

    return {
        "chat": {"id": chat_id, "title": await chat_title(_bot(request), chat_id)},
        "period": span.as_json(),
        "periods": list(periods.PERIOD_KEYS),
        "total": total,
        "items": items,
        "days": days,
        "previous": comparison,
        "categories": [{"name": name, "total": value, "count": n} for name, value, n in categories],
        "top": [{"name": name, "total": value, "count": n} for name, value, n in top],
        "timeline": _timeline(daily, span),
    }


def _timeline(daily: list[tuple[str, float]], span: periods.Period) -> list[dict]:
    """
    Ряд для графика.

    По дням отдаём как есть — нули дорисует фронтенд, ему всё равно нужно знать
    границы периода. По месяцам схлопываем здесь: гонять год поденных значений
    ради двенадцати столбиков незачем.
    """
    if span.step == "day":
        return [{"at": day, "total": value} for day, value in daily]

    buckets: dict[str, float] = collections.defaultdict(float)
    for day, value in daily:
        buckets[day[:7]] += value
    return [{"at": month, "total": value} for month, value in sorted(buckets.items())]
