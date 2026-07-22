"""Роутеры бота. Порядок важен: команды разбираются раньше свободного текста."""
from handlers.commands import router as commands_router
from handlers.messages import router as messages_router

__all__ = ["commands_router", "messages_router"]
