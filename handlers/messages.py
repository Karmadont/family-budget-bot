"""
handlers/messages.py — обработка обычных сообщений чата.

Текстовое сообщение:
  1. Это обращение к боту? -> отвечаем на вопрос.
  2. Есть ли в тексте цифры? Нет -> молча игнорируем (экономим вызовы API).
  3. Отдаём текст Claude: покупка или нет. Не покупка -> молчим.
  4. Покупка -> пишем в базу и подтверждаем.

Фотография (или картинка файлом):
  скачиваем -> приводим к JPEG нужного размера -> Claude читает чек -> в базу.
"""
from __future__ import annotations

import logging
import re
import time

from aiogram import F, Router
from aiogram.types import Message, ReactionTypeEmoji

import claude_client
import config
import db
import images
import services
from handlers.commands import answer_question
from models import ParsedMessage

log = logging.getLogger(__name__)
router = Router(name="messages")

HAS_DIGIT = re.compile(r"\d")
# Обращение к боту: «бот, ...», «Бот ...», «эй бот».
ADDRESS_RE = re.compile(r"^\s*(?:эй[ ,]+)?бот[\s,:!?]+", re.IGNORECASE)
MAX_TEXT_LEN = 1500
# Сколько позиций показывать в подтверждении, прежде чем свернуть в «и ещё N».
CONFIRM_ITEM_LIMIT = 15
# Как часто ругаться на сломанную настройку (кончились деньги, неверный ключ).
# Без паузы бот отвечал бы этим на каждое сообщение с цифрами.
WARN_COOLDOWN_SEC = 600
_last_warned: dict[int, float] = {}


def _may_warn(chat_id: int) -> bool:
    now = time.monotonic()
    if now - _last_warned.get(chat_id, float("-inf")) < WARN_COOLDOWN_SEC:
        return False
    _last_warned[chat_id] = now
    return True


# --- текстовые сообщения ----------------------------------------------------

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

    try:
        parsed = await claude_client.parse_message(text, services.today().isoformat())
    except claude_client.ClaudeError as exc:
        # Настройка сломана. Молчать нельзя — иначе бот просто «не работает»,
        # и непонятно почему. Но и на каждое сообщение отвечать не будем.
        if _may_warn(message.chat.id):
            await message.reply(str(exc))
        return

    if not parsed.is_purchase or not parsed.items:
        return

    await _save(message, parsed, text)


# --- фотографии чеков -------------------------------------------------------

def _image_file(message: Message) -> tuple[str | None, int]:
    """Найти в сообщении картинку. -> (file_id, размер в байтах)"""
    if message.photo:
        # photo — список превью разного размера, последний самый крупный.
        largest = message.photo[-1]
        return largest.file_id, largest.file_size or 0
    document = message.document
    if document and (document.mime_type or "").startswith("image/"):
        return document.file_id, document.file_size or 0
    return None, 0


@router.message(F.photo | F.document.mime_type.startswith("image/"))
async def on_photo(message: Message) -> None:
    if not config.READ_RECEIPTS:
        return
    if message.from_user and message.from_user.is_bot:
        return

    file_id, file_size = _image_file(message)
    if file_id is None:
        return

    if file_size > config.MAX_IMAGE_MB * 1024 * 1024:
        await message.reply(
            f"Картинка больше {config.MAX_IMAGE_MB:g} МБ — я такую не скачаю. "
            "Отправьте её обычным фото, а не файлом."
        )
        return

    status = await message.reply("🧾 Читаю чек…")
    try:
        buffer = await message.bot.download(file_id)
        if buffer is None:
            raise RuntimeError("Telegram не отдал файл")
        raw = buffer.read()
    except Exception:  # noqa: BLE001 — сетевые сбои Telegram не должны ронять бота
        log.exception("Не удалось скачать картинку")
        await status.edit_text("Не смог скачать фото из Telegram. Попробуйте отправить ещё раз.")
        return

    try:
        prepared, media_type = images.prepare(raw)
    except images.UnreadableImage:
        log.exception("Картинка не открылась")
        await status.edit_text("Не смог открыть этот файл как картинку.")
        return

    log.info("Чек: %.0f КБ -> %.0f КБ после подготовки", len(raw) / 1024, len(prepared) / 1024)

    try:
        parsed = await claude_client.parse_receipt(
            prepared, media_type, message.caption, services.today().isoformat()
        )
    except claude_client.ClaudeError as exc:
        await status.edit_text(str(exc))
        return

    if not parsed.is_purchase or not parsed.items:
        note = f"\n<i>{services.esc(parsed.note)}</i>" if parsed.note else ""
        await status.edit_text(f"Не увидел на фото чек с покупками.{note}")
        return

    await _save(message, parsed, message.caption or "[фото чека]", status=status)


# --- общая часть ------------------------------------------------------------

async def _save(
    message: Message,
    parsed: ParsedMessage,
    raw_text: str,
    status: Message | None = None,
) -> None:
    """Записать разобранную покупку и подтвердить в чате."""
    saved = await db.save_parsed(
        chat_id=message.chat.id,
        message_id=message.message_id,
        user_id=message.from_user.id if message.from_user else None,
        user_name=message.from_user.full_name if message.from_user else None,
        raw_text=raw_text,
        parsed=parsed,
        default_date=services.today().isoformat(),
    )
    if not saved:
        return

    log.info("chat=%s сохранено позиций: %s", message.chat.id, saved)

    # Для фото уже висит сообщение «Читаю чек…» — его и правим.
    if status is not None:
        await status.edit_text(_confirm_text(parsed))
        return

    if config.CONFIRM_MODE == "quiet":
        return
    if config.CONFIRM_MODE == "reaction":
        try:
            await message.react([ReactionTypeEmoji(emoji="👍")])
            return
        except Exception:  # noqa: BLE001 — реакции доступны не во всех чатах
            log.debug("Не удалось поставить реакцию, отвечаю текстом")

    await message.reply(_confirm_text(parsed))


def _confirm_text(parsed: ParsedMessage) -> str:
    """Подтверждение записи. Длинные чеки сворачиваем, чтобы влезть в сообщение."""
    total = sum(item.price for item in parsed.items)
    header = f"✅ Записал {len(parsed.items)} поз. на {services.money(total)}"
    if parsed.store:
        header += f" · {services.esc(parsed.store)}"
    lines = [header]

    for item in parsed.items[:CONFIRM_ITEM_LIMIT]:
        qty = f"{item.quantity:g} {item.unit}".strip() if item.quantity is not None else ""
        qty_part = f" ({services.esc(qty)})" if qty else ""
        lines.append(
            f"• {services.esc(item.name)}{qty_part} — {services.money(item.price)}"
            f" <i>{services.esc(item.category)}</i>"
        )
    hidden = len(parsed.items) - CONFIRM_ITEM_LIMIT
    if hidden > 0:
        lines.append(f"<i>…и ещё {hidden} поз.</i>")

    # Расхождение с ИТОГО чека — почти всегда значит, что позицию прочитали неверно.
    if parsed.total is not None and abs(parsed.total - total) >= 1:
        lines.append(
            f"⚠️ В чеке ИТОГО {services.money(parsed.total)}, "
            f"а по позициям {services.money(total)}. Проверьте, при ошибке — /undo"
        )
    if parsed.note:
        lines.append(f"<i>{services.esc(parsed.note)}</i>")

    return "\n".join(lines)
