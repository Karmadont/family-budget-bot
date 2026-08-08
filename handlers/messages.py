"""
handlers/messages.py — обработка сообщений чата.

Текстовое сообщение (главный поток — сбор данных для статистики):
  1. Это ответ на уточняющий вопрос бота? -> склеиваем с исходным текстом и разбираем.
  2. Это обращение к боту? -> отвечаем на вопрос.
  3. Есть ли в тексте цифры? Нет -> молча игнорируем (экономим вызовы API).
  4. Отдаём текст модели: покупка или нет. Не покупка -> молчим.
  5. Покупка разобрана -> пишем в базу и подтверждаем.
  6. Видно, что покупка, но записать нечего -> задаём один уточняющий вопрос.

Проверка на ответ идёт первой не случайно: reply на сообщение бота иначе
считался бы свободным вопросом к нейросети, и ответ «продукты на 2000» ушёл бы
не в базу, а в болталку.

Фото чека (второстепенная функция, только по команде /receipt):
  скачиваем -> приводим к JPEG -> OCR + разбор -> в базу.
Просто присланное фото бот НЕ трогает — иначе он лез бы в каждую картинку в чате.
"""
from __future__ import annotations

import logging
import re
import time

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.methods import TelegramMethod
from aiogram.types import Message, ReactionTypeEmoji

import config
import db
import images
import llm
import retry
import services
from handlers.commands import answer_question
from models import ParsedMessage

log = logging.getLogger(__name__)
router = Router(name="messages")

HAS_DIGIT = re.compile(r"\d")
# Слова, при которых сообщение стоит отдать модели, даже если цифр в нём нет:
# «купил продуктов» — это отчёт о покупке, просто без суммы, и правильная
# реакция на него — спросить сумму, а не промолчать. Список нарочно короткий:
# каждое лишнее слово здесь — это лишние вызовы платного API на болтовню в чате.
BUY_HINT = re.compile(
    r"\b(?:купил|купила|купили|взял|взяла|взяли|потратил|потратила|потратили"
    r"|заказал|заказала|заказали|заплатил|заплатила|заплатили"
    r"|оплатил|оплатила|оплатили|закупил|закупилась|закупились|затарил)",
    re.IGNORECASE,
)
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

    # Личка с ботом — любое сообщение без цифр считаем вопросом. Кроме отчётов
    # о покупке без суммы: «купил продуктов» — это не вопрос, а повод спросить цену.
    if message.chat.type == "private" and not HAS_DIGIT.search(text):
        return None if BUY_HINT.search(text) else text

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

    if await _answer_clarification(message, text):
        return

    me = await message.bot.me()
    question = _extract_question(message, me.username)
    if question:
        await answer_question(message, question)
        return

    # Дешёвый фильтр перед платным вызовом: либо в тексте есть цифры, либо он
    # похож на отчёт о покупке без суммы — во втором случае бот спросит сумму.
    if len(text) > MAX_TEXT_LEN:
        return
    if not HAS_DIGIT.search(text) and not BUY_HINT.search(text):
        return

    # Обе проверки стоят до вызова модели: повтор нужно отсечь раньше, чем за его
    # разбор будут списаны деньги.
    if await _skip_duplicate(message, text):
        return

    try:
        parsed, spent = await llm.parse_message(text, services.today().isoformat())
    except llm.LLMError as exc:
        # Настройка сломана. Молчать нельзя — иначе бот просто «не работает»,
        # и непонятно почему. Но и на каждое сообщение отвечать не будем.
        if _may_warn(message.chat.id):
            await message.reply(str(exc))
        return

    await db.log_usage(message.chat.id, spent)

    if not parsed.is_purchase:
        return

    if parsed.items:
        await _save(message, parsed, text)
        return

    # Покупка была, но записывать нечего: не названа цена, непонятно за что.
    # Тогда — ровно один вопрос, и ждём ответа reply'ем.
    if parsed.clarify:
        await _ask_clarification(message, text, parsed.clarify)


# --- уточняющие вопросы ------------------------------------------------------

async def _ask_clarification(message: Message, raw_text: str, question: str) -> None:
    """Задать вопрос и запомнить, на что человек будет отвечать."""
    ask = await message.reply(f"❓ {services.esc(question)}")

    # Чистим просроченные тут же: вопросы редки, и отдельная фоновая задача
    # ради пары строк в неделю не нужна.
    dropped = await db.purge_pending()
    if dropped:
        log.info("Убрал %s забытых уточняющих вопросов", dropped)

    await db.add_pending(
        chat_id=message.chat.id,
        message_id=message.message_id,
        ask_message_id=ask.message_id,
        user_id=message.from_user.id if message.from_user else None,
        user_name=message.from_user.full_name if message.from_user else None,
        raw_text=raw_text,
        question=question,
    )
    log.info("chat=%s задал уточняющий вопрос: %s", message.chat.id, question)


async def _answer_clarification(message: Message, text: str) -> bool:
    """
    Обработать ответ на уточняющий вопрос. -> был ли это ответ.

    Разбираем исходное сообщение вместе с ответом: по отдельности «купил
    продуктов» и «на 2000» не разбираются ни то, ни другое.
    """
    reply = message.reply_to_message
    if reply is None:
        return False

    row = await db.pending_by_ask(message.chat.id, reply.message_id)
    if row is None:
        return False

    # Вопрос закрыт в любом случае: даже если разобрать не удалось, второй раз
    # спрашивать не будем — иначе бот и человек уйдут в переписку по кругу.
    await db.drop_pending(row["id"])

    combined = f"{row['raw_text']}\n{text}"
    try:
        parsed, spent = await llm.parse_message(combined, services.today().isoformat())
    except llm.LLMError as exc:
        if _may_warn(message.chat.id):
            await message.reply(str(exc))
        return True

    await db.log_usage(message.chat.id, spent)

    if not parsed.items:
        await message.reply(
            "Всё равно не понял, что записать. Напишите покупку с ценами: "
            "<i>молоко 89, хлеб 45</i>"
        )
        return True

    # Привязываем к исходному сообщению и его автору: покупку сделал он, а
    # уточнить мог кто угодно из чата.
    await _save(
        message, parsed, combined,
        message_id=row["message_id"],
        user_id=row["user_id"],
        user_name=row["user_name"],
    )
    return True


# --- фото чека по команде /receipt ------------------------------------------

def _image_file(message: Message | None) -> tuple[str | None, int]:
    """Найти в сообщении картинку. -> (file_id, размер в байтах)"""
    if message is None:
        return None, 0
    if message.photo:
        # photo — список превью разного размера, последний самый крупный.
        largest = message.photo[-1]
        return largest.file_id, largest.file_size or 0
    document = message.document
    if document and (document.mime_type or "").startswith("image/"):
        return document.file_id, document.file_size or 0
    return None, 0


@router.message(Command("receipt", "чек"))
async def cmd_receipt(message: Message, command: CommandObject) -> None:
    """
    Разобрать фото чека. Вызывается принудительно, двумя способами:
      • фото с подписью «/receipt» (или «/чек»);
      • «/receipt» в ответ на уже отправленное фото.
    """
    if not config.READ_RECEIPTS:
        await message.reply("Разбор чеков выключен. Включите READ_RECEIPTS=true в .env.")
        return

    # Картинка либо в этом же сообщении (подпись-команда), либо в том, на которое отвечают.
    source = message if _image_file(message)[0] else message.reply_to_message
    file_id, file_size = _image_file(source)
    if file_id is None:
        await message.reply(
            "Пришлите фото чека с подписью <code>/receipt</code> "
            "или ответьте <code>/receipt</code> на уже отправленное фото."
        )
        return

    if file_size > config.MAX_IMAGE_MB * 1024 * 1024:
        await message.reply(
            f"Картинка больше {config.MAX_IMAGE_MB:g} МБ — я такую не скачаю. "
            "Отправьте её обычным фото, а не файлом."
        )
        return

    # Подпись-подсказка: текст после команды или подпись исходного фото.
    hint = command.args or (source.caption if source is not message else None)

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
        parsed, spent = await llm.parse_receipt(
            prepared, media_type, hint, services.today().isoformat()
        )
    except llm.LLMError as exc:
        await status.edit_text(str(exc))
        return

    await db.log_usage(message.chat.id, spent)

    if not parsed.is_purchase or not parsed.items:
        note = f"\n<i>{services.esc(parsed.note)}</i>" if parsed.note else ""
        await status.edit_text(f"Не увидел на фото чек с покупками.{note}")
        return

    await _save(message, parsed, hint or "[фото чека]", status=status)


# --- общая часть ------------------------------------------------------------

async def _save(
    message: Message,
    parsed: ParsedMessage,
    raw_text: str,
    status: Message | None = None,
    *,
    message_id: int | None = None,
    user_id: int | None = None,
    user_name: str | None = None,
    source: str = "chat",
) -> None:
    """
    Записать разобранную покупку и подтвердить в чате.

    message_id/user_id/user_name можно передать явно: при ответе на уточняющий
    вопрос покупку надо привязать к исходному сообщению, а не к ответу.
    """
    author = message.from_user
    saved = await db.save_parsed(
        chat_id=message.chat.id,
        message_id=message.message_id if message_id is None else message_id,
        user_id=(author.id if author else None) if user_id is None else user_id,
        user_name=(author.full_name if author else None) if user_name is None else user_name,
        raw_text=raw_text,
        parsed=parsed,
        default_date=services.today().isoformat(),
        source=source,
    )
    if not saved:
        return

    log.info("chat=%s сохранено позиций: %s", message.chat.id, saved)

    # Для фото уже висит сообщение «Читаю чек…» — его и правим.
    if status is not None:
        await _deliver(message, status.edit_text(_confirm_text(parsed)), what="разбор чека")
        return

    # Молчаливые режимы отменяются, если есть что проверить: беззвучно записать
    # покупку в наугад выбранную категорию — как раз то, на что потом ругаются.
    doubtful = any(item.uncertain for item in parsed.items)
    if not doubtful:
        if config.CONFIRM_MODE == "quiet":
            return
        if config.CONFIRM_MODE == "reaction":
            try:
                await message.react([ReactionTypeEmoji(emoji="👍")])
                return
            except Exception:  # noqa: BLE001 — реакции доступны не во всех чатах
                log.debug("Не удалось поставить реакцию, отвечаю текстом")

    await _deliver(message, message.reply(_confirm_text(parsed)), what="подтверждение покупки")


async def _deliver(message: Message, method: TelegramMethod, *, what: str) -> None:
    """
    Довести сообщение до чата, переживая обрывы связи.

    Покупка к этому моменту уже в базе, и уронить обработчик из-за недоставленного
    подтверждения было бы худшим из вариантов: деньги учтены, человек об этом не
    знает, а в логах трейсбек вместо внятной строчки.

    Если не дошло даже после повторов — не страшно. Человек напишет то же самое
    ещё раз, `_skip_duplicate` узнает повтор, второй записи не будет, а
    подтверждение уйдёт заново.
    """
    try:
        await retry.send(message.bot, method, what=what)
    except TelegramAPIError as exc:
        log.warning("chat=%s не доставил %s: %s", message.chat.id, what, exc)


# --- защита от повторной записи ----------------------------------------------

async def _skip_duplicate(message: Message, text: str) -> bool:
    """
    Отсечь повторную запись той же покупки. -> True, если сообщение пропущено.

    Повторы бывают двух видов, и реакция на них разная.

    Апдейт прислал заново сам Telegram — так бывает, когда бот не успел
    подтвердить получение из-за обрыва связи. Человек ничего не делал и
    подтверждение уже видел: молчим, иначе в чате появится второе «Записал».

    Человек написал то же самое сам — почти наверняка потому, что подтверждения
    не увидел. Второй раз записывать нельзя, а вот показать, что покупка на месте,
    как раз и нужно: именно за этим он и пришёл.
    """
    chat_id = message.chat.id

    if await db.message_saved(chat_id, message.message_id):
        log.info("chat=%s апдейт для сообщения %s уже обработан — пропускаю",
                 chat_id, message.message_id)
        return True

    twin = await db.recent_twin(chat_id, text, config.DUPLICATE_WINDOW_MIN)
    if twin is None:
        return False

    rows = await db.rows_for_message(chat_id, twin)
    log.info("chat=%s сообщение повторяет %s (%s поз.) — переотправляю подтверждение",
             chat_id, twin, len(rows))
    await _deliver(message, message.reply(_repeat_text(rows)), what="повторное подтверждение")
    return True


def _item_line(name: str, quantity: float | None, unit: str | None,
               price: float, category: str, uncertain: bool) -> str:
    """Строка одной позиции. Одна на два подтверждения — из разбора и из базы."""
    qty = f"{quantity:g} {unit}".strip() if quantity is not None else ""
    qty_part = f" ({services.esc(qty)})" if qty else ""
    # Знак вопроса у позиции = категорию или цену модель выбрала наугад.
    mark = " ❓" if uncertain else ""
    return (f"• {services.esc(name)}{qty_part} — {services.money(price)}"
            f" <i>{services.esc(category)}</i>{mark}")


def _repeat_text(rows: list[db.Purchase]) -> str:
    """Подтверждение, собранное из уже записанных строк, — ответ на повтор."""
    total = sum(row.price for row in rows)
    store = next((row.store for row in rows if row.store), None)
    header = f"✅ Это уже записано: {len(rows)} поз. на {services.money(total)}"
    if store:
        header += f" · {services.esc(store)}"

    lines = [header]
    lines += [_item_line(row.name, row.quantity, row.unit, row.price,
                         row.category, row.needs_review)
              for row in rows[:CONFIRM_ITEM_LIMIT]]
    hidden = len(rows) - CONFIRM_ITEM_LIMIT
    if hidden > 0:
        lines.append(f"<i>…и ещё {hidden} поз.</i>")
    lines.append("<i>Повторно не записал. Если это правда вторая такая покупка — "
                 f"добавьте её через {config.DUPLICATE_WINDOW_MIN} мин. или напишите иначе.</i>")
    return "\n".join(lines)


def _confirm_text(parsed: ParsedMessage) -> str:
    """Подтверждение записи. Длинные чеки сворачиваем, чтобы влезть в сообщение."""
    total = sum(item.price for item in parsed.items)
    header = f"✅ Записал {len(parsed.items)} поз. на {services.money(total)}"
    if parsed.store:
        header += f" · {services.esc(parsed.store)}"

    lines = [header]
    lines += [_item_line(item.name, item.quantity, item.unit, item.price,
                         item.category, item.uncertain)
              for item in parsed.items[:CONFIRM_ITEM_LIMIT]]
    hidden = len(parsed.items) - CONFIRM_ITEM_LIMIT
    if hidden > 0:
        lines.append(f"<i>…и ещё {hidden} поз.</i>")

    doubtful = sum(1 for item in parsed.items if item.uncertain)
    if doubtful:
        lines.append(
            f"❓ {doubtful} поз. под вопросом — поправьте в /app, "
            "там они собраны отдельным списком."
        )

    # Расхождение с ИТОГО чека — почти всегда значит, что позицию прочитали неверно.
    if parsed.total is not None and abs(parsed.total - total) >= 1:
        lines.append(
            f"⚠️ В чеке ИТОГО {services.money(parsed.total)}, "
            f"а по позициям {services.money(total)}. Проверьте, при ошибке — /undo"
        )
    # clarify сюда попадает, только если что-то записать всё же удалось: когда
    # разобрать нечего, бот задаёт вопрос вместо подтверждения.
    if parsed.clarify:
        lines.append(f"❓ {services.esc(parsed.clarify)}")
    if parsed.note:
        lines.append(f"<i>{services.esc(parsed.note)}</i>")

    return "\n".join(lines)
