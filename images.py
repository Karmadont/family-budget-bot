"""
images.py — подготовка фотографии чека к распознаванию (Yandex Vision OCR).

Зачем это нужно:
  * фото с телефона бывает 12 Мп и 8 МБ — в запрос такое класть незачем;
  * снимки часто повёрнуты (ориентация лежит в EXIF, а не в пикселях);
  * HEIC/PNG/WebP приводим к одному формату.
"""
from __future__ import annotations

import io

from PIL import Image, ImageOps

# Длинную сторону режем до 2576 px: для распознавания чека этого с запасом
# хватает, а платить и ждать за лишние пиксели смысла нет.
MAX_EDGE = 2576
# Предел на одну картинку в запросе к API.
MAX_BYTES = 4_500_000
START_QUALITY = 88
MIN_QUALITY = 45


class UnreadableImage(ValueError):
    """Файл не открылся как картинка."""


def prepare(raw: bytes) -> tuple[bytes, str]:
    """
    Привести картинку к JPEG нужного размера.

    -> (байты, media_type) для отправки в OCR.
    """
    try:
        image = Image.open(io.BytesIO(raw))
        # Развернуть по EXIF: иначе чек, снятый вертикально, уедет набок,
        # и модель будет читать его боком.
        image = ImageOps.exif_transpose(image) or image
        image = image.convert("RGB")
    except Exception as exc:  # noqa: BLE001 — Pillow бросает что угодно на битых файлах
        raise UnreadableImage(str(exc)) from exc

    if max(image.size) > MAX_EDGE:
        image.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)

    quality = START_QUALITY
    while True:
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
        if buffer.tell() <= MAX_BYTES or quality <= MIN_QUALITY:
            return buffer.getvalue(), "image/jpeg"
        quality -= 15
