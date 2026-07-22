"""
handlers/commands.py — команды бота.
"""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BufferedInputFile, Message

import claude_client
import config
import db
import services
from models import CATEGORIES

log = logging.getLogger(__name__)
router = Router(name="commands")

HELP = """<b>Как мной пользоваться</b>

Просто пишите в чат, что купили и почём — я разберу и запишу:
<i>молоко 89, хлеб 45, куриное филе 1.2кг 420</i>
<i>потратил 3500 в Пятёрочке</i>

Спросить что угодно — ответьте на моё сообщение или начните со слова «бот»:
<i>бот, сколько мы потратили на мясо в этом месяце?</i>
<i>бот, что приготовить на ужин?</i>

<b>Команды</b>
/stats — расходы по категориям (<code>/stats неделя</code>, <code>месяц</code>, <code>год</code>, <code>всё</code>, <code>30</code>)
/fridge — что, скорее всего, лежит дома
/recipe — что приготовить из этого (<code>/recipe быстро и без мяса</code>)
/ask — вопрос по покупкам (<code>/ask на что ушло больше всего?</code>)
/ate — отметить съеденное (<code>/ate молоко</code>)
/undo — удалить последнюю записанную покупку
/export — выгрузить всё в CSV
/categories — список категорий
/chatid — id этого чата (для ALLOWED_CHAT_IDS)"""


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Я записываю ваши покупки, считаю статистику и подсказываю, "
        f"что приготовить.\n\nID этого чата: <code>{message.chat.id}</code>\n\n{HELP}"
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("chatid"))
async def cmd_chatid(message: Message) -> None:
    await message.answer(f"ID этого чата: <code>{message.chat.id}</code>")


@router.message(Command("categories"))
async def cmd_categories(message: Message) -> None:
    listing = "\n".join(f"• {c}" for c in CATEGORIES)
    await message.answer(f"<b>Категории</b>\n{listing}\n\nМеняются в <code>models.py</code>.")


@router.message(Command("stats"))
async def cmd_stats(message: Message, command: CommandObject) -> None:
    since, until, label = services.parse_period(command.args)
    await message.answer(await services.stats_report(message.chat.id, since, until, label))


@router.message(Command("fridge"))
async def cmd_fridge(message: Message) -> None:
    await message.answer(await services.fridge_report(message.chat.id))


@router.message(Command("recipe"))
async def cmd_recipe(message: Message, command: CommandObject) -> None:
    items = await services.fridge_items(message.chat.id)
    if not items:
        await message.answer(
            "Не знаю, что у вас есть — за последние "
            f"{config.FRIDGE_WINDOW_DAYS} дн. продукты не записывались."
        )
        return

    await message.bot.send_chat_action(message.chat.id, "typing")
    try:
        answer = await claude_client.suggest_recipes(services.fridge_as_text(items), command.args)
    except claude_client.ClaudeError as exc:
        await message.answer(str(exc))
        return

    for part in services.chunks(answer):
        await message.answer(part)


@router.message(Command("ask"))
async def cmd_ask(message: Message, command: CommandObject) -> None:
    question = (command.args or "").strip()
    if not question:
        await message.answer("Напишите вопрос: <code>/ask сколько ушло на кафе в этом месяце?</code>")
        return
    await answer_question(message, question)


@router.message(Command("ate"))
async def cmd_ate(message: Message, command: CommandObject) -> None:
    needle = (command.args or "").strip()
    if not needle:
        await message.answer("Что съели? Например: <code>/ate молоко</code>")
        return

    updated = await db.mark_consumed(message.chat.id, needle)
    if updated:
        await message.answer(f"Отметил как съеденное: {services.esc(needle)} ({updated} поз.)")
    else:
        await message.answer(f"Не нашёл «{services.esc(needle)}» среди несъеденного.")


@router.message(Command("undo"))
async def cmd_undo(message: Message) -> None:
    count, total = await db.delete_last_message(message.chat.id)
    if count:
        await message.answer(f"Удалил последнюю запись: {count} поз. на {services.money(total)}")
    else:
        await message.answer("Удалять нечего — записей ещё нет.")


@router.message(Command("export"))
async def cmd_export(message: Message) -> None:
    data = await services.export_csv(message.chat.id)
    if len(data) < 100:  # только заголовок
        await message.answer("Пока нечего выгружать.")
        return
    await message.answer_document(
        BufferedInputFile(data, filename=f"purchases_{services.today()}.csv"),
        caption="Все покупки этого чата.",
    )


async def answer_question(message: Message, question: str) -> None:
    """Общая точка для /ask и обращений к боту текстом."""
    await message.bot.send_chat_action(message.chat.id, "typing")
    context = await services.build_context(message.chat.id)
    try:
        answer = await claude_client.ask(question, context)
    except claude_client.ClaudeError as exc:
        await message.answer(str(exc))
        return

    for part in services.chunks(answer):
        await message.answer(part)
