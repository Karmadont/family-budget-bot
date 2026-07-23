"""
providers — реализации провайдеров нейросети.

Провайдеры создаются лениво и по одному экземпляру: клиенты держат пул
соединений и токены, плодить их на каждый запрос незачем.
"""
from __future__ import annotations

from providers.base import LLMError, Provider, Reply, TransientError
from usage import CLAUDE, GIGACHAT, YANDEXGPT

_cache: dict[str, Provider] = {}


def _build(name: str) -> Provider:
    if name == CLAUDE:
        from providers.claude import ClaudeProvider

        return ClaudeProvider()
    if name == YANDEXGPT:
        from providers.yandex import YandexProvider

        return YandexProvider()
    if name == GIGACHAT:
        from providers.gigachat import GigaChatProvider

        return GigaChatProvider()
    raise LLMError(f"Неизвестный провайдер: {name}")


def get(name: str) -> Provider:
    """Провайдер по имени из .env (claude | yandexgpt | gigachat)."""
    if name not in _cache:
        _cache[name] = _build(name)
    return _cache[name]


async def close_all() -> None:
    """Закрыть соединения всех созданных провайдеров."""
    for provider in _cache.values():
        await provider.aclose()
    _cache.clear()


__all__ = ["LLMError", "Provider", "Reply", "TransientError", "close_all", "get"]
