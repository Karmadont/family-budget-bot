"""
tools/import_chat.py — разобрать историю чата из выгрузки Telegram Desktop.

Бот видит только те сообщения, что пришли после его добавления в чат. Всё, что
писалось раньше, для статистики не существует. Этот скрипт закрывает пробел:
берёт официальную выгрузку чата и прогоняет старые сообщения через тот же разбор,
что работает в боте.

КАК ПОЛУЧИТЬ ВЫГРУЗКУ
  Telegram Desktop (в мобильном приложении такого нет):
  открыть чат -> ⋮ -> «Экспорт истории чата» -> снять все галочки с медиа
  (нужен только текст) -> формат «JSON» -> Экспортировать.
  Получится папка с файлом result.json.

ЗАПУСК
  # сначала посмотреть, что будет разобрано, без обращений к нейросети и без записи
  python tools/import_chat.py --file ~/Downloads/ChatExport/result.json --chat-id -1001234567890 --dry-run

  # затем сам импорт (спросит подтверждение и покажет оценку стоимости)
  python tools/import_chat.py --file ~/Downloads/ChatExport/result.json --chat-id -1001234567890

ВАЖНО
  • Остановите бота на время импорта: два процесса, пишущих в один файл SQLite,
    рано или поздно поймают «database is locked».
  • Разбор каждого сообщения — платный вызов Yandex Cloud. На истории за год это
    может быть несколько тысяч вызовов, поэтому скрипт сначала считает оценку.
  • Повторный запуск не задваивает записи: сообщения, уже попавшие в базу,
    пропускаются по message_id.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Скрипт лежит в tools/, а модули бота — на уровень выше.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import db  # noqa: E402
import llm  # noqa: E402
import services  # noqa: E402
import usage as usage_mod  # noqa: E402
from handlers.messages import BUY_HINT, HAS_DIGIT, MAX_TEXT_LEN  # noqa: E402

log = logging.getLogger("import")

# Сколько сообщений разбирать одновременно. Больше — быстрее, но у Yandex Cloud
# есть лимит запросов в секунду, а 429 здесь означает потерянную работу.
CONCURRENCY = 4

# Оценка: сколько символов приходится на один токен в русском тексте. Считаем
# грубо и в запас — цель не точная цифра, а порядок величины перед тратой денег.
CHARS_PER_TOKEN = 2.5
# Средний размер ответа модели на одно сообщение, токенов.
OUTPUT_TOKENS_GUESS = 180


@dataclass(slots=True)
class Candidate:
    """Сообщение из выгрузки, похожее на отчёт о покупке."""

    message_id: int
    date: str          # YYYY-MM-DD
    author: str | None
    author_id: int | None
    text: str


@dataclass(slots=True)
class Totals:
    """Что получилось по итогам импорта."""

    parsed: int = 0
    saved_rows: int = 0
    not_purchase: int = 0
    unclear: int = 0
    failed: int = 0
    review: int = 0
    cost: float = 0.0
    unclear_texts: list[str] = field(default_factory=list)
    # Сообщения, разбор которых сорвался: их не отмечаем обработанными, чтобы
    # следующий запуск попробовал снова.
    failed_ids: set[int] = field(default_factory=set)


# --- чтение выгрузки ---------------------------------------------------------

def message_text(raw: dict) -> str:
    """
    Собрать текст сообщения.

    В выгрузке поле text — либо строка, либо список кусков: обычный текст лежит
    строками, а ссылки, упоминания и жирный — объектами {type, text}. Склеиваем
    всё подряд, разметка для разбора цен не важна.
    """
    text = raw.get("text")
    if isinstance(text, str):
        return text
    if not isinstance(text, list):
        return ""

    parts = []
    for piece in text:
        if isinstance(piece, str):
            parts.append(piece)
        elif isinstance(piece, dict):
            parts.append(str(piece.get("text", "")))
    return "".join(parts)


def author_id(raw: dict) -> int | None:
    """'user123456789' -> 123456789. Каналы и боты дают другой формат — тогда None."""
    match = re.fullmatch(r"user(\d+)", str(raw.get("from_id", "")))
    return int(match.group(1)) if match else None


def expected_chat_ids(info: dict) -> list[int]:
    """
    Каким мог бы быть chat_id этого чата с точки зрения бота.

    В выгрузке id лежит без знака, а Telegram отдаёт боту отрицательный: у
    обычной группы это -<id>, у супергруппы и канала — -100<id>. Личная
    переписка остаётся положительной. Возвращаем все правдоподобные варианты,
    чтобы предупредить об опечатке, но не мешать, если человек знает лучше.
    """
    raw = info.get("id")
    if not isinstance(raw, int):
        return []
    kind = str(info.get("type", ""))
    if "supergroup" in kind or "channel" in kind:
        return [int(f"-100{raw}")]
    if "group" in kind:
        return [-raw]
    return [raw]


def looks_like_purchase(text: str) -> bool:
    """Тот же дешёвый фильтр, что в боте: цифры либо слово о покупке."""
    if not text or len(text) > MAX_TEXT_LEN:
        return False
    if text.lstrip().startswith("/"):
        return False
    return bool(HAS_DIGIT.search(text) or BUY_HINT.search(text))


def read_candidates(path: Path, since: str | None, limit: int | None) -> tuple[list[Candidate], dict]:
    """Прочитать выгрузку и отобрать сообщения, которые стоит разбирать."""
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        sys.exit(f"Нет файла {path}")
    except json.JSONDecodeError as exc:
        sys.exit(f"{path} — не похоже на JSON выгрузки Telegram: {exc}")

    messages = data.get("messages")
    if not isinstance(messages, list):
        sys.exit(
            f"В {path} нет списка messages. Проверьте, что выгрузка сделана в формате JSON, "
            "и что это именно result.json из папки экспорта."
        )

    info = {"name": data.get("name"), "id": data.get("id"), "type": data.get("type"),
            "total": len(messages)}

    picked: list[Candidate] = []
    for raw in messages:
        # service — это «такой-то вошёл в группу» и прочая служебная запись.
        if raw.get("type") != "message":
            continue
        text = message_text(raw).strip()
        if not looks_like_purchase(text):
            continue

        date = str(raw.get("date", ""))[:10]
        if len(date) != 10:
            continue
        if since and date < since:
            continue

        picked.append(Candidate(
            message_id=int(raw.get("id", 0)),
            date=date,
            author=raw.get("from"),
            author_id=author_id(raw),
            text=text,
        ))
        if limit and len(picked) >= limit:
            break

    return picked, info


# --- оценка стоимости --------------------------------------------------------

def estimate(candidates: list[Candidate]) -> tuple[int, float]:
    """Прикинуть, во сколько обойдётся разбор. -> (токенов, рублей)"""
    import prompts

    system_chars = len(prompts.PARSER_SYSTEM)
    tokens = 0
    for item in candidates:
        # На каждое сообщение уходит системный промпт целиком плюс сам текст.
        tokens += int((system_chars + len(item.text) + 40) / CHARS_PER_TOKEN)
        tokens += OUTPUT_TOKENS_GUESS

    rate = usage_mod.rate_for(config.YANDEX_PARSER_MODEL)
    return tokens, tokens * rate / 1_000_000


# --- импорт ------------------------------------------------------------------

async def import_one(item: Candidate, chat_id: int, totals: Totals, gate: asyncio.Semaphore) -> None:
    async with gate:
        try:
            parsed, spent = await llm.parse_message(item.text, item.date)
        except llm.LLMError as exc:
            # Ошибка настройки или кончились деньги — дальше нет смысла.
            raise SystemExit(f"\nРазбор остановлен: {exc}") from exc
        except Exception:  # noqa: BLE001 — одно плохое сообщение не должно рушить импорт
            log.exception("Сообщение %s разобрать не удалось", item.message_id)
            totals.failed += 1
            totals.failed_ids.add(item.message_id)
            return

    totals.parsed += 1
    if spent:
        await db.log_usage(chat_id, spent)
        totals.cost += sum(entry.cost for entry in spent)

    if not parsed.is_purchase:
        totals.not_purchase += 1
        return

    if not parsed.items:
        # Покупка была, но цены нет. В чате бот спросил бы; здесь спрашивать
        # некого, поэтому просто откладываем такие тексты в отчёт.
        totals.unclear += 1
        totals.unclear_texts.append(f"{item.date}  {item.text}")
        return

    # Дату берём из выгрузки: bought_on из текста («вчера») относился бы к
    # сегодняшнему дню, а сообщение написано год назад.
    saved = await db.save_parsed(
        chat_id=chat_id,
        message_id=item.message_id,
        user_id=item.author_id,
        user_name=item.author,
        raw_text=item.text,
        parsed=parsed,
        default_date=item.date,
        source="import",
    )
    totals.saved_rows += saved
    totals.review += sum(1 for one in parsed.items if one.uncertain)


async def run(args: argparse.Namespace) -> int:
    candidates, info = read_candidates(Path(args.file).expanduser(), args.since, args.limit)

    print(f"Выгрузка: {info['name']!r} (id {info['id']}, тип {info['type']})")
    print(f"Всего записей в файле: {info['total']}")
    print(f"Похожи на покупку: {len(candidates)}")

    plausible = expected_chat_ids(info)
    if plausible and args.chat_id not in plausible:
        print(f"\n⚠️  Вы указали --chat-id {args.chat_id}, а по выгрузке ожидался "
              f"{' или '.join(str(one) for one in plausible)}.")
        print("   В выгрузке id лежит без знака, а бот видит его отрицательным:")
        print("   у обычной группы это -<id>, у супергруппы и канала -100<id>.")
        print("   Точный ответ даёт команда /chatid в самом чате.")
        print("   Если чат не тот, покупки уедут в чужую статистику.")

    if not candidates:
        print("\nРазбирать нечего.")
        return 0

    await db.init()
    try:
        return await _import(args, candidates)
    finally:
        # Закрываем всегда, в том числе на ранних выходах. Соединение aiosqlite
        # держит фоновый поток, и без close() процесс печатает результат и
        # повисает — именно так вёл себя --dry-run.
        await db.close()
        await llm.close()


async def _import(args: argparse.Namespace, candidates: list[Candidate]) -> int:
    """Сверка с базой, оценка, подтверждение и сам разбор."""
    known = await db.known_message_ids(args.chat_id)
    fresh = [item for item in candidates if item.message_id not in known]
    skipped = len(candidates) - len(fresh)
    if skipped:
        print(f"Уже в базе (пропущу): {skipped}")
    if not fresh:
        print("\nВсё это уже импортировано.")
        return 0

    tokens, rubles = estimate(fresh)
    span = f"{fresh[0].date} … {fresh[-1].date}" if fresh else "—"
    print(f"\nК разбору: {len(fresh)} сообщений, период {span}")
    print(f"Оценка: ~{tokens:,} токенов ≈ {rubles:.1f} ₽ "
          f"(модель {config.YANDEX_PARSER_MODEL}, ставка из .env)".replace(",", " "))

    if args.dry_run:
        print("\n--dry-run: ничего не разбираю и не пишу. Первые 10 сообщений:")
        for item in fresh[:10]:
            print(f"  {item.date}  {item.author or '?'}: {item.text[:100]}")
        return 0

    if not args.yes:
        print("\nЭто платные обращения к Yandex Cloud.")
        if input("Продолжить? [y/N] ").strip().lower() not in ("y", "yes", "д", "да"):
            print("Отменено.")
            return 1

    totals = Totals()
    gate = asyncio.Semaphore(CONCURRENCY)
    started = dt.datetime.now()

    # Пачками, чтобы печатать прогресс и не создавать тысячи задач разом.
    batch_size = CONCURRENCY * 10
    for start in range(0, len(fresh), batch_size):
        batch = fresh[start : start + batch_size]
        await asyncio.gather(*(import_one(item, args.chat_id, totals, gate) for item in batch))
        # Отмечаем пачкой после разбора: сообщения, не оказавшиеся покупками, не
        # оставят строк в purchases, и без этой отметки повторный запуск оплатил
        # бы их разбор заново. Сбои не отмечаем — их стоит попробовать ещё раз.
        await db.mark_imported(
            args.chat_id,
            (one.message_id for one in batch if one.message_id not in totals.failed_ids),
        )
        done = min(start + batch_size, len(fresh))
        elapsed = (dt.datetime.now() - started).total_seconds()
        print(f"  {done}/{len(fresh)}  записано строк: {totals.saved_rows}"
              f"  потрачено: {totals.cost:.2f} ₽  ({elapsed:.0f} с)")

    print("\n--- итог ---")
    print(f"Разобрано сообщений:      {totals.parsed}")
    print(f"Записано позиций:         {totals.saved_rows}")
    print(f"Из них под вопросом:      {totals.review}  (в приложении — фильтр «Проверить»)")
    print(f"Не покупка:               {totals.not_purchase}")
    print(f"Покупка без цены:         {totals.unclear}")
    print(f"Сбоев разбора:            {totals.failed}")
    print(f"Фактический расход:       {totals.cost:.2f} ₽")

    if totals.unclear_texts and args.unclear:
        report = Path(args.unclear).expanduser()
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("\n".join(totals.unclear_texts) + "\n", encoding="utf-8")
        print(f"\nСообщения о покупках без цены выписаны в {report} — "
              "их можно дописать в чат руками.")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Импорт истории чата из JSON-выгрузки Telegram Desktop.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Подробности и порядок действий — в докстринге этого файла и в README.",
    )
    parser.add_argument("--file", required=True, help="путь к result.json из выгрузки")
    parser.add_argument("--chat-id", type=int, required=True,
                        help="id чата, как его видит бот (команда /chatid в этом чате)")
    parser.add_argument("--since", help="разбирать только с этой даты, YYYY-MM-DD")
    parser.add_argument("--limit", type=int, help="взять не больше N сообщений (для пробы)")
    parser.add_argument("--dry-run", action="store_true",
                        help="показать, что будет разобрано, без вызовов API и записи")
    parser.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    parser.add_argument("--unclear", default="data/import-unclear.txt",
                        help="куда выписать покупки без цены (пусто — не выписывать)")
    args = parser.parse_args()

    if args.since:
        try:
            dt.date.fromisoformat(args.since)
        except ValueError:
            sys.exit("--since должен быть датой вида 2025-01-31")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # services импортирован ради побочного эффекта: он тянет config и проверяет .env
    # раньше, чем мы начнём читать многомегабайтный JSON.
    _ = services.today()

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nПрервано. Уже записанные покупки остались в базе.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
