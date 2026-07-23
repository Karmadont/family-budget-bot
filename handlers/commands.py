"""
handlers/commands.py — команды бота.
"""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BufferedInputFile, Message

import config
import db
import llm
import services
import usage as usage_mod
from models import CATEGORIES

log = logging.getLogger(__name__)
router = Router(name="commands")

HELP = """<b>Как мной пользоваться</b>

Просто пишите в чат, что купили и почём — я разберу и запишу:
<i>молоко 89, хлеб 45, куриное филе 1.2кг 420</i>
<i>потратил 3500 в Пятёрочке</i>

Или киньте <b>фото чека</b> — разнесу по позициям сам. Длинный чек лучше
отправлять файлом, а не фото: так не потеряется мелкий шрифт.

Спросить что угодно — ответьте на моё сообщение или начните со слова «бот»:
<i>бот, сколько мы потратили на мясо в этом месяце?</i>
<i>бот, что приготовить на ужин?</i>

<b>Команды</b>
/stats — расходы по категориям (<code>/stats неделя</code>, <code>месяц</code>, <code>год</code>, <code>всё</code>, <code>30</code>)
/fridge — что, скорее всего, лежит дома
/recipe — что приготовить из этого (<code>/recipe быстро и без мяса</code>)
/ask — вопрос по покупкам (<code>/ask на что ушло больше всего?</code>)
/ate — отметить съеденное (<code>/ate молоко</code>)
/cost — сколько потрачено на нейросеть
/undo — удалить последнюю записанную покупку
/export — выгрузить всё в CSV
/categories — список категорий
/provider — какая нейросеть сейчас работает
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


@router.message(Command("provider"))
async def cmd_provider(message: Message) -> None:
    """Какая нейросеть сейчас разбирает сообщения и читает чеки."""
    chat, vision = llm.chat_provider(), llm.vision_provider()
    lines = [
        "<b>Нейросеть</b>",
        f"Текст и вопросы: <b>{services.esc(usage_mod.PROVIDER_LABELS.get(chat.name, chat.name))}</b>",
    ]
    lines += [f"• {services.esc(line)}" for line in _models_of(chat.name)]

    if config.READ_RECEIPTS:
        title = usage_mod.PROVIDER_LABELS.get(vision.name, vision.name)
        how = "смотрит на фото" if vision.supports_images else "распознаёт текст через OCR"
        if not vision.reads_receipts:
            how = "чеки читать не умеет"
        lines += ["", f"Чеки: <b>{services.esc(title)}</b> — {how}"]
    else:
        lines += ["", "Чеки: <i>выключено (READ_RECEIPTS=false)</i>"]

    lines += ["", "Переключается в <code>.env</code>: LLM_PROVIDER, VISION_PROVIDER.",
              "Сравнить, во что обошёлся каждый: /cost"]
    await message.answer("\n".join(lines))


def _models_of(provider: str) -> list[str]:
    """Модели выбранного провайдера — чтобы не лезть в .env ради проверки."""
    if provider == usage_mod.YANDEXGPT:
        return [f"вопросы: {config.YANDEX_MODEL}", f"разбор: {config.YANDEX_PARSER_MODEL}"]
    if provider == usage_mod.GIGACHAT:
        return [f"вопросы: {config.GIGACHAT_MODEL}", f"разбор: {config.GIGACHAT_PARSER_MODEL}"]
    return [f"вопросы: {config.CLAUDE_MODEL}", f"разбор: {config.CLAUDE_PARSER_MODEL}"]


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
        answer, spent = await llm.suggest_recipes(services.fridge_as_text(items), command.args)
    except llm.LLMError as exc:
        await message.answer(str(exc))
        return

    await db.log_usage(message.chat.id, spent)
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


@router.message(Command("cost"))
async def cmd_cost(message: Message, command: CommandObject) -> None:
    since, until, label = services.parse_period(command.args)
    await message.answer(await services.cost_report(message.chat.id, since, until, label))


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
        answer, spent = await llm.ask(question, context)
    except llm.LLMError as exc:
        await message.answer(str(exc))
        return

    await db.log_usage(message.chat.id, spent)
    for part in services.chunks(answer):
        await message.answer(part)
