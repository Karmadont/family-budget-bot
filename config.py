"""
config.py — конфигурация бота.

Все настройки читаются из файла .env рядом с этим файлом (см. .env.example).
Файл .env в .gitignore и НЕ попадает в репозиторий.

Нейросеть одна — YandexGPT. Разбор текста о покупках и еженедельный анализ трат
идут через text-модель, чеки — через Yandex Vision OCR плюс ту же модель.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path
from typing import NoReturn
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")


def _fail(message: str) -> NoReturn:
    print(f"[config] {message}")
    sys.exit(1)


def _env(name: str, default: str = "") -> str:
    """
    Как os.getenv, но пустое значение считается «не задано».

    В .env.example необязательные поля стоят пустыми (`DB_PATH=`), и os.getenv
    вернул бы для них пустую строку вместо значения по умолчанию.
    """
    return os.getenv(name, "").strip() or default


def _required(name: str) -> str:
    """Обязательная переменная окружения: без неё бот не стартует."""
    value = _env(name)
    if not value:
        _fail(f"Не задана обязательная переменная {name}. Заполните .env (см. .env.example).")
    return value


def _int(name: str, default: str) -> int:
    raw = _env(name, default)
    try:
        return int(raw)
    except ValueError:
        _fail(f"{name}='{raw}' — нужно целое число.")


def _float(name: str, default: str) -> float:
    raw = _env(name, default)
    try:
        return float(raw)
    except ValueError:
        _fail(f"{name}='{raw}' — нужно число.")


def _flag(name: str, default: str) -> bool:
    return _env(name, default).lower() not in ("0", "false", "no", "off")


def _time(name: str, default: str) -> dt.time:
    """Разобрать 'ЧЧ:ММ' в объект времени."""
    raw = _env(name, default)
    try:
        hour, minute = (int(part) for part in raw.split(":", 1))
        return dt.time(hour, minute)
    except (ValueError, TypeError):
        _fail(f"{name}='{raw}' — нужно время в формате ЧЧ:ММ, например 10:00.")


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

# --- Yandex Cloud (YandexGPT) ---
# Оба значения из консоли Yandex Cloud: API-ключ сервисного аккаунта и id каталога.
YANDEX_API_KEY = _required("YANDEX_API_KEY")
YANDEX_FOLDER_ID = _required("YANDEX_FOLDER_ID")
# Имя модели с версией — подставляется в modelUri: gpt://<folder>/<model>.
YANDEX_MODEL = _env("YANDEX_MODEL", "yandexgpt/latest")
# Разбор коротких сообщений о покупках — самый частый вызов, тут дешёвая модель уместна.
YANDEX_PARSER_MODEL = _env("YANDEX_PARSER_MODEL", "yandexgpt-lite/latest")
# Разбор распознанного текста чека и еженедельный анализ.
YANDEX_VISION_MODEL = _env("YANDEX_VISION_MODEL", YANDEX_MODEL)
YANDEX_OCR_MODEL = _env("YANDEX_OCR_MODEL", "page")
# Таймаут HTTP-запроса к Yandex Cloud, секунд.
LLM_TIMEOUT = _float("LLM_TIMEOUT", "120")
# Рублей за миллион токенов (см. комментарий в usage.py — это оценка, впишите свою).
YANDEX_PRICE_LITE = _float("YANDEX_PRICE_LITE", "200")
YANDEX_PRICE_PRO = _float("YANDEX_PRICE_PRO", "1200")
# Рублей за одну распознанную страницу чека.
YANDEX_OCR_PRICE_PER_PAGE = _float("YANDEX_OCR_PRICE_PER_PAGE", "0.26")

# --- Еженедельный дайджест (главная функция) ---
# Каждую неделю бот сам выкладывает разбор трат за прошлую неделю.
WEEKLY_DIGEST = _flag("WEEKLY_DIGEST", "true")
# День недели публикации: 0 — понедельник … 6 — воскресенье.
WEEKLY_DIGEST_WEEKDAY = _int("WEEKLY_DIGEST_WEEKDAY", "0")
if not 0 <= WEEKLY_DIGEST_WEEKDAY <= 6:
    _fail("WEEKLY_DIGEST_WEEKDAY — число от 0 (понедельник) до 6 (воскресенье).")
# Время публикации по TIMEZONE.
WEEKLY_DIGEST_TIME = _time("WEEKLY_DIGEST_TIME", "10:00")
# Добавлять ли к цифрам короткий анализ от нейросети (тренд, что необычно).
WEEKLY_DIGEST_ANALYSIS = _flag("WEEKLY_DIGEST_ANALYSIS", "true")

# --- Поведение ---
CURRENCY = _env("CURRENCY", "₽")
_TZ_NAME = _env("TIMEZONE", "Europe/Moscow")
try:
    TIMEZONE = ZoneInfo(_TZ_NAME)
except Exception:  # noqa: BLE001 — ZoneInfoNotFoundError и всё, что зависит от ОС
    _fail(f"TIMEZONE='{_TZ_NAME}' — не нашёл такой часовой пояс. Пример: Europe/Moscow")
# Сколько дней покупка продуктов считается «лежит в холодильнике».
FRIDGE_WINDOW_DAYS = _int("FRIDGE_WINDOW_DAYS", "10")
# Сколько последних покупок отдавать модели как контекст для свободных вопросов.
CONTEXT_PURCHASES_LIMIT = _int("CONTEXT_PURCHASES_LIMIT", "150")
# Как подтверждать сохранение покупки: reply (текстом) | reaction (эмодзи) | quiet
CONFIRM_MODE = _env("CONFIRM_MODE", "reply").lower()
# Разрешён ли разбор фото чеков командой /receipt.
READ_RECEIPTS = _flag("READ_RECEIPTS", "true")
# Предел размера входящей картинки, МБ (у Telegram Bot API свой потолок — 20 МБ).
MAX_IMAGE_MB = _float("MAX_IMAGE_MB", "20")

# --- Хранилище ---
# Относительный путь считаем от папки проекта, а не от текущей директории:
# под systemd бот запускается неизвестно откуда.
DB_PATH = Path(_env("DB_PATH", "data/bot.sqlite3")).expanduser()
if not DB_PATH.is_absolute():
    DB_PATH = PROJECT_ROOT / DB_PATH
try:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
except OSError as exc:
    _fail(f"Не могу создать папку для базы {DB_PATH.parent}: {exc}")

LOG_LEVEL = _env("LOG_LEVEL", "INFO").upper()
