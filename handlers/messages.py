"""
handlers/messages.py — обработка обычных сообщений чата.

Логика одного сообщения:
  1. Это обращение к боту? -> отвечаем на вопрос.
  2. Есть ли в тексте цифры? Нет -> молча игнорируем (экономим вызовы API).
  3. Отдаём текст Claude: покупка или нет. Не покупка -> молчим.
  4. Покупка -> пишем в базу и подтверждаем.
"""
from __future__ import annotations

import logging
import re

from aiogram import F, Router
from aiogram.types import Message, ReactionTypeEmoji

import claude_client
import config
import db
import services
from handlers.commands import answer_question

log = logging.getLogger(__name__)
router = Router(name="messages")

HAS_DIGIT = re.compile(r"\d")
# Обращение к боту: «бот, ...», «Бот ...», «эй бот».
ADDRESS_RE = re.compile(r"^\s*(?:эй[ ,]+)?бот[\s,:!?]+", re.IGNORECASE)
MAX_TEXT_LEN = 1500


def _extract_question(message: Message, bot_username: str | None) -> str | None:
    """Вернуть текст вопроса, если сообщение адресовано боту, иначе None."""
    text = (message.text or "").strip()
    if not text:
        return None

    # Личка с ботом — любое сообщение без цифр считаем вопросом.
    if message.chat.type == "private" and not HAS_DIGIT.search(text):
        return text

    # Ответ на сообщение бота.
    reply = message.reply_to_message
    if reply is not None and reply.from_user is not None and reply.from_user.is_bot:
        if bot_username and reply.from_user.username == bot_username:
            return text

    # Упоминание @username.
    if bot_username and f"@{bot_username}".lower() in text.lower():
        cleaned = re.sub(rf"@{re.escape(bot_username)}", "", text, flags=re.IGNORECASE).strip()
        return cleaned or None

    # Обращение словом «бот».
    if ADDRESS_RE.match(text):
        cleaned = ADDRESS_RE.sub("", text).strip()
        return cleaned or None

    return None


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message) -> None:
    text = (message.text or "").strip()
    if not text or (message.from_user and message.from_user.is_bot):
        return

    me = await message.bot.me()
    question = _extract_question(message, me.username)
    if question:
        await answer_question(message, question)
        return

    # Дешёвый фильтр перед платным вызовом: без цифр покупки не бывает.
    if not HAS_DIGIT.search(text) or len(text) > MAX_TEXT_LEN:
        return

    parsed = await claude_client.parse_message(text, services.today().isoformat())
    if not parsed.is_purchase or not parsed.items:
        return

    saved = await db.save_parsed(
        chat_id=message.chat.id,
        message_id=message.message_id,
        user_id=message.from_user.id if message.from_user else None,
        user_name=message.from_user.full_name if message.from_user else None,
        raw_text=text,
        parsed=parsed,
        default_date=services.today().isoformat(),
    )
    if not saved:
        return

    log.info("chat=%s сохранено позиций: %s", message.chat.id, saved)
    await _confirm(message, parsed)


async def _confirm(message: Message, parsed) -> None:
    """Подтвердить сохранение так, как настроено в CONFIRM_MODE."""
    if config.CONFIRM_MODE == "quiet":
        return

    if config.CONFIRM_MODE == "reaction":
        try:
            await message.react([ReactionTypeEmoji(emoji="👍")])
            return
        except Exception:  # реакции доступны не во всех чатах — падаем в текстовый режим
            log.debug("Не удалось поставить реакцию, отвечаю текстом")

    total = sum(item.price for item in parsed.items)
    lines = [f"✅ Записал, {services.money(total)}"]
    for item in parsed.items:
        qty = f"{item.quantity:g} {item.unit}".strip() if item.quantity is not None else ""
        qty_part = f" ({services.esc(qty)})" if qty else ""
        lines.append(
            f"• {services.esc(item.name)}{qty_part} — {services.money(item.price)}"
            f" <i>{services.esc(item.category)}</i>"
        )
    if parsed.store:
        lines.append(f"<i>магазин: {services.esc(parsed.store)}</i>")
    if parsed.note:
        lines.append(f"<i>{services.esc(parsed.note)}</i>")

    await message.reply("\n".join(lines))
