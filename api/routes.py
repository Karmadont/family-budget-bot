"""
api/routes.py — эндпоинты мини-приложения.

Каждый ответ — JSON с сырыми числами: форматированием занимается фронтенд, ему
всё равно нужно уметь это делать для анимаций и переключения периодов.

Единственная дверь внутрь — зависимость `access`: без проверенной подписи
Telegram и членства в чате ни один эндпоинт с данными не отработает.
"""
from __future__ import annotations

import collections
import datetime as dt
import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

import config
import db
import llm
import services
from api import periods
from api.auth import Access, AuthError, authorize, is_member, pack_chat, parse_init_data
from models import CATEGORIES, Category

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
        # Список категорий нужен приложению для выпадающего списка при правке.
        # Отдаём здесь, чтобы не делать под него отдельный запрос.
        "categories": list(CATEGORIES),
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
        # Счётчик нужен здесь, а не только в ленте: значок на вкладке «Траты»
        # должен быть виден сразу, иначе пометка «проверить» замечается только
        # тем, кто и так туда зашёл.
        "review": await db.review_count(chat_id),
        "previous": comparison,
        "categories": [{"name": name, "total": value, "count": n} for name, value, n in categories],
        "top": [{"name": name, "total": value, "count": n} for name, value, n in top],
        "timeline": _timeline(daily, span),
    }


# --- лента покупок ------------------------------------------------------------

PAGE_LIMIT = 50

# Столбцы purchases, объявленные NOT NULL: null в них присылать нельзя.
# Обнулять можно только quantity, unit и store.
NOT_NULL_FIELDS = ("name", "category", "price", "bought_at",
                   "is_food", "perishable", "needs_review")


def _parse_cursor(raw: str | None) -> tuple[str, int] | None:
    """'2026-07-01:123' -> ('2026-07-01', 123). Мусор считаем отсутствием курсора."""
    if not raw:
        return None
    date, _, ident = raw.rpartition(":")
    try:
        return date, int(ident)
    except ValueError:
        return None


@router.get("/purchases")
async def purchases(
    who: Access = Depends(access),
    period: str | None = Query(default=None),
    category: str | None = Query(default=None),
    review: bool = Query(default=False, description="только записи, требующие проверки"),
    search: str | None = Query(default=None, max_length=100),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=PAGE_LIMIT, ge=1, le=200),
) -> dict:
    """Лента покупок, новые сверху. Листается курсором из поля next."""
    chat_id = who.chat_id
    span = None
    if period:
        earliest = await db.first_purchase_date(chat_id) if period == "all" else None
        span = periods.resolve(period, earliest)

    # Просим на одну строку больше запрошенного: так узнаём, есть ли следующая
    # страница, не делая для этого второй запрос с COUNT.
    rows = await db.purchases_page(
        chat_id,
        since=span.since if span else None,
        until=span.until if span else None,
        category=category,
        review_only=review,
        search=search,
        cursor=_parse_cursor(cursor),
        limit=limit + 1,
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = f"{rows[-1].bought_at}:{rows[-1].id}" if has_more and rows else None

    return {
        "items": [row.as_json() for row in rows],
        "next": next_cursor,
        "review": await db.review_count(chat_id),
        "period": span.as_json() if span else None,
    }


class PurchasePatch(BaseModel):
    """
    Что разрешено менять в записи.

    Незаданные поля не трогаем — отличить «не присылали» от «присылали null»
    позволяет exclude_unset при выгрузке. Категория объявлена типом Category,
    то есть списком из models.py: значение не из списка отклонит сам FastAPI.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    category: Category | None = None
    price: float | None = Field(default=None, ge=0, le=10_000_000)
    quantity: float | None = Field(default=None, ge=0)
    unit: str | None = Field(default=None, max_length=20)
    bought_at: dt.date | None = None
    store: str | None = Field(default=None, max_length=100)
    is_food: bool | None = None
    perishable: bool | None = None
    needs_review: bool | None = None


@router.patch("/purchases/{purchase_id}")
async def edit_purchase(
    patch: PurchasePatch,
    purchase_id: int = Path(ge=1),
    who: Access = Depends(access),
) -> dict:
    """Поправить запись — то, для чего в основном и нужен этот экран."""
    changes = patch.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(400, "Нечего менять.")

    # В базе эти столбцы NOT NULL. Присланный явным образом null дошёл бы до
    # SQLite и превратился в 500 — отвечаем понятной ошибкой раньше.
    for field in NOT_NULL_FIELDS:
        if field in changes and changes[field] is None:
            raise HTTPException(400, f"Поле «{field}» не может быть пустым.")

    if isinstance(changes.get("bought_at"), dt.date):
        changes["bought_at"] = changes["bought_at"].isoformat()
    if (name := changes.get("name")) is not None:
        changes["name"] = name.strip()
        if not changes["name"]:
            raise HTTPException(400, "Название не может быть пустым.")
    for flag in ("is_food", "perishable", "needs_review"):
        if flag in changes:
            changes[flag] = int(changes[flag])

    # Правка и есть проверка: человек только что посмотрел на запись глазами.
    # Если он хочет оставить пометку — пусть присылает needs_review явно.
    changes.setdefault("needs_review", 0)

    if not await db.update_purchase(who.chat_id, purchase_id, changes):
        # Либо записи нет, либо она из другого чата — снаружи это одно и то же.
        raise HTTPException(404, "Запись не найдена.")

    updated = await db.get_purchase(who.chat_id, purchase_id)
    log.info("chat=%s user=%s поправил запись %s: %s",
             who.chat_id, who.user_id, purchase_id, sorted(changes))
    return {"item": updated.as_json() if updated else None,
            "review": await db.review_count(who.chat_id)}


@router.delete("/purchases/{purchase_id}")
async def remove_purchase(
    purchase_id: int = Path(ge=1),
    who: Access = Depends(access),
) -> dict:
    if not await db.delete_purchase(who.chat_id, purchase_id):
        raise HTTPException(404, "Запись не найдена.")
    log.info("chat=%s user=%s удалил запись %s", who.chat_id, who.user_id, purchase_id)
    return {"deleted": purchase_id, "review": await db.review_count(who.chat_id)}


# --- уточняющие вопросы -------------------------------------------------------


@router.get("/pending")
async def pending(who: Access = Depends(access)) -> dict:
    """Вопросы, на которые бот ждёт ответа. В чате их можно закрыть reply'ем."""
    rows = await db.pending_list(who.chat_id)
    return {
        "items": [
            {"id": row["id"], "question": row["question"], "raw_text": row["raw_text"],
             "user_name": row["user_name"], "created_at": row["created_at"]}
            for row in rows
        ]
    }


class PendingAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1, max_length=1000)


@router.post("/pending/{pending_id}/answer")
async def answer_pending(
    body: PendingAnswer,
    pending_id: int = Path(ge=1),
    who: Access = Depends(access),
) -> dict:
    """
    Ответить на уточняющий вопрос из приложения.

    Логика та же, что при ответе reply'ем в чате: разбираем исходный текст
    вместе с ответом, потому что по отдельности не разбирается ни то, ни другое.
    """
    row = await db.pending_get(who.chat_id, pending_id)
    if row is None:
        raise HTTPException(404, "Вопрос не найден — возможно, на него уже ответили.")

    combined = f"{row['raw_text']}\n{body.answer.strip()}"
    try:
        parsed, spent = await llm.parse_message(combined, services.today().isoformat())
    except llm.LLMError as exc:
        raise HTTPException(503, str(exc)) from exc

    await db.log_usage(who.chat_id, spent)
    # Вопрос закрываем в любом случае: переспрашивать по кругу не будем.
    await db.drop_pending(pending_id)

    if not parsed.items:
        return {"saved": 0, "message": "Из ответа так и не вышло покупки с ценой."}

    saved = await db.save_parsed(
        chat_id=who.chat_id,
        message_id=row["message_id"],
        user_id=row["user_id"],
        user_name=row["user_name"],
        raw_text=combined,
        parsed=parsed,
        default_date=services.today().isoformat(),
    )
    log.info("chat=%s ответ из приложения на вопрос %s: записано %s поз.",
             who.chat_id, pending_id, saved)
    return {"saved": saved, "review": await db.review_count(who.chat_id)}


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
