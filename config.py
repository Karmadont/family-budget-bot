"""
config.py — конфигурация бота.

Все настройки читаются из файла .env рядом с этим файлом (см. .env.example).
Файл .env в .gitignore и НЕ попадает в репозиторий.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")


def _required(name: str) -> str:
    """Обязательная переменная окружения: без неё бот не стартует."""
    value = os.getenv(name, "").strip()
    if not value:
        print(f"[config] Не задана обязательная переменная {name}. Заполните .env (см. .env.example).")
        sys.exit(1)
    return value


def _int_set(name: str) -> set[int]:
    """Список chat_id через запятую -> множество int."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return set()
    result: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            result.add(int(chunk))
        except ValueError:
            print(f"[config] {name}: '{chunk}' не похоже на chat_id (нужно целое число). Пропускаю.")
    return result


# --- Telegram ---
TELEGRAM_BOT_TOKEN = _required("TELEGRAM_BOT_TOKEN")
# Белый список чатов: бот работает только в них. Пусто = отвечает везде,
# куда его добавили (небезопасно, если токен куда-то утечёт).
ALLOWED_CHAT_IDS = _int_set("ALLOWED_CHAT_IDS")

# --- Anthropic (Claude API) ---
ANTHROPIC_API_KEY = _required("ANTHROPIC_API_KEY")
# Модель для ответов на вопросы и рецептов.
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-4-8")
# Модель для разбора сообщений о покупках. Это самый частый вызов —
# сюда имеет смысл поставить модель подешевле (см. README, раздел «Стоимость»).
CLAUDE_PARSER_MODEL = os.getenv("CLAUDE_PARSER_MODEL", CLAUDE_MODEL)

# --- Поведение ---
CURRENCY = os.getenv("CURRENCY", "₽")
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
# Сколько дней покупка продуктов считается «лежит в холодильнике».
FRIDGE_WINDOW_DAYS = int(os.getenv("FRIDGE_WINDOW_DAYS", "10"))
# Сколько последних покупок отдавать модели как контекст для свободных вопросов.
CONTEXT_PURCHASES_LIMIT = int(os.getenv("CONTEXT_PURCHASES_LIMIT", "150"))
# Как подтверждать сохранение покупки: reply (текстом) | reaction (эмодзи) | quiet
CONFIRM_MODE = os.getenv("CONFIRM_MODE", "reply").strip().lower()

# --- Хранилище ---
DB_PATH = Path(os.getenv("DB_PATH", str(PROJECT_ROOT / "data" / "bot.sqlite3")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
