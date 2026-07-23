"""
scheduler.py — еженедельная рассылка разбора трат.

Раз в неделю (по умолчанию — понедельник утром) бот сам публикует в каждый
активный чат сводку расходов за прошлую неделю. Это главная функция бота:
статистика приходит без запроса, а не только по команде.

Реализация нарочно простая: одна фоновая задача, которая спит до ближайшего
времени публикации и просыпается. Никакой персистентности — если бот в этот
момент был выключен, неделя пропускается (следующая придёт как обычно).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from aiogram import Bot

import config
import db
import services

log = logging.getLogger(__name__)


def next_run(now: dt.datetime) -> dt.datetime:
    """Ближайший момент публикации после `now` (в часовом поясе бота)."""
    target = config.WEEKLY_DIGEST_TIME
    days_ahead = (config.WEEKLY_DIGEST_WEEKDAY - now.weekday()) % 7
    candidate = now.replace(
        hour=target.hour, minute=target.minute, second=0, microsecond=0
    ) + dt.timedelta(days=days_ahead)
    # Если сегодня нужный день, но время уже прошло — переносим на следующую неделю.
    if candidate <= now:
        candidate += dt.timedelta(days=7)
    return candidate


async def run(bot: Bot) -> None:
    """Фоновый цикл: спим до времени публикации, рассылаем, повторяем."""
    if not config.WEEKLY_DIGEST:
        log.info("Еженедельный дайджест выключен (WEEKLY_DIGEST=false).")
        return

    while True:
        now = dt.datetime.now(config.TIMEZONE)
        target = next_run(now)
        delay = (target - now).total_seconds()
        log.info("Следующий дайджест: %s (через %.1f ч)", target.strftime("%d.%m %H:%M"), delay / 3600)
        await asyncio.sleep(delay)

        try:
            await _post_all(bot)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — сбой рассылки не должен убить цикл
            log.exception("Не удалось разослать еженедельный дайджест")


async def _post_all(bot: Bot) -> None:
    """Разослать дайджест во все активные чаты (с учётом белого списка)."""
    chats = await db.distinct_chats()
    if config.ALLOWED_CHAT_IDS:
        chats = [c for c in chats if c in config.ALLOWED_CHAT_IDS]

    log.info("Рассылаю еженедельный дайджест в %d чат(ов)", len(chats))
    for chat_id in chats:
        try:
            digest = await services.weekly_digest(chat_id)
            if digest is None:
                continue  # за неделю ничего не куплено — молчим
            for part in services.chunks(digest):
                await bot.send_message(chat_id, part)
        except Exception:  # noqa: BLE001 — бота могли удалить из чата и т.п.
            log.exception("Дайджест для чата %s не отправлен", chat_id)
