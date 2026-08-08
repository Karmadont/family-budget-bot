"""
bot.py — точка входа.

Запуск:  python bot.py
Остановка: Ctrl+C

В одном процессе живут три вещи: поллинг Telegram, еженедельная рассылка и —
если задан WEBAPP_URL — веб-сервер мини-приложения. Разносить их по процессам
нельзя: соединение с SQLite в db.py одно на процесс, и два писателя в один файл
рано или поздно поймают «database is locked».
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, Message

import config
import db
import llm
import retry
import scheduler
from handlers import commands_router, messages_router

log = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand(command="app", description="Открыть приложение с графиками"),
    BotCommand(command="digest", description="Разбор трат за прошлую неделю"),
    BotCommand(command="stats", description="Расходы по категориям"),
    BotCommand(command="ask", description="Вопрос по покупкам"),
    BotCommand(command="cost", description="Расходы на нейросеть"),
    BotCommand(command="receipt", description="Разобрать фото чека"),
    BotCommand(command="fridge", description="Что лежит дома"),
    BotCommand(command="recipe", description="Что приготовить"),
    BotCommand(command="ate", description="Отметить съеденное"),
    BotCommand(command="undo", description="Удалить последнюю запись"),
    BotCommand(command="export", description="Выгрузить в CSV"),
    BotCommand(command="help", description="Справка"),
]


class ChatGuard(BaseMiddleware):
    """Пускаем в работу только чаты из ALLOWED_CHAT_IDS."""

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        if config.ALLOWED_CHAT_IDS and event.chat.id not in config.ALLOWED_CHAT_IDS:
            log.warning("Сообщение из чужого чата %s — игнорирую", event.chat.id)
            # Подсказываем id один раз, по команде — иначе бот молчит.
            if (event.text or "").startswith(("/start", "/chatid")):
                await event.answer(
                    f"Этот чат не в белом списке.\nID: <code>{event.chat.id}</code>\n"
                    "Добавьте его в ALLOWED_CHAT_IDS в .env и перезапустите бота."
                )
            return None
        return await handler(event, data)


async def main() -> None:
    logging.basicConfig(
        level=config.LOG_LEVEL,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    await db.init()

    bot = Bot(
        token=config.TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.message.outer_middleware(ChatGuard())
    dispatcher.include_router(commands_router)
    dispatcher.include_router(messages_router)

    # Первый же вызов идёт в Telegram, и на нестабильной связи он может не дойти.
    # Без повтора процесс на этом падал: контейнер перезапускался хостингом и
    # всё равно поднимался, но каждый такой круг — минута, когда бот не работает.
    me = await retry.call(
        lambda: bot.get_me(request_timeout=int(config.TELEGRAM_TIMEOUT)),
        what="проверка токена при старте",
        attempts=max(config.TELEGRAM_RETRIES, 5),
    )
    log.info("Запущен как @%s (модель: %s)", me.username, config.YANDEX_MODEL)
    if not config.ALLOWED_CHAT_IDS:
        log.warning("ALLOWED_CHAT_IDS пуст — бот ответит в любом чате, куда его добавили.")
    if config.WEEKLY_DIGEST and not config.ALLOWED_CHAT_IDS:
        log.info("Дайджест придёт во все чаты, где записаны покупки.")

    # Меню команд — украшение: оно уже установлено с прошлого запуска и живёт на
    # стороне Telegram. Ронять из-за него бота нельзя.
    try:
        await retry.call(
            lambda: bot.set_my_commands(BOT_COMMANDS,
                                        request_timeout=int(config.TELEGRAM_TIMEOUT)),
            what="обновление меню команд",
        )
    except TelegramAPIError as exc:
        log.warning("Меню команд не обновилось: %s. Работаю дальше.", exc)

    # Еженедельная рассылка живёт своей фоновой задачей рядом с поллингом.
    digest_task = asyncio.create_task(scheduler.run(bot))

    # Веб-сервер поднимаем, только если приложению есть куда смотреть.
    web_server = web_task = None
    if config.WEBAPP_URL:
        from api.app import create_server  # fastapi нужен только здесь

        web_server = create_server(bot)
        web_task = asyncio.create_task(web_server.serve())
        log.info("Мини-приложение: %s", config.WEBAPP_URL)
    else:
        log.info("WEBAPP_URL не задан — мини-приложение выключено.")

    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        digest_task.cancel()
        try:
            await digest_task
        except asyncio.CancelledError:
            pass
        if web_server is not None:
            # should_exit вместо cancel(): uvicorn успеет доответить на открытые
            # запросы и закрыть сокет по-человечески.
            web_server.should_exit = True
            await web_task
        await bot.session.close()
        await llm.close()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nОстановлен.")
