"""
bot.py — точка входа.

Запуск:  python bot.py
Остановка: Ctrl+C
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, Message

import config
import db
from handlers import commands_router, messages_router

log = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand(command="stats", description="Расходы по категориям"),
    BotCommand(command="fridge", description="Что лежит дома"),
    BotCommand(command="recipe", description="Что приготовить"),
    BotCommand(command="ask", description="Вопрос по покупкам"),
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

    me = await bot.me()
    log.info("Запущен как @%s (модель: %s)", me.username, config.CLAUDE_MODEL)
    if not config.ALLOWED_CHAT_IDS:
        log.warning("ALLOWED_CHAT_IDS пуст — бот ответит в любом чате, куда его добавили.")

    await bot.set_my_commands(BOT_COMMANDS)
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nОстановлен.")
