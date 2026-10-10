"""Persist Nano Banana 2.1 image bytes before marking generation completed."""
from __future__ import annotations

import base64
import binascii
import hashlib
import io

from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.media.models import MediaAsset
from app.media.service import build_partner_media_url
from app.media.storage import get_media_storage


async def persist_single_image(db: AsyncSession, generation: Generation, data: dict) -> MediaAsset:
    items = data.get("data")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise ValueError("image_result_count_unrecoverable")
    encoded = items[0].get("b64_json")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("image_result_missing_bytes")
    settings = get_settings()
    maximum = settings.media_image_max_bytes
    if len(encoded) > 4 * ((maximum + 2) // 3):
        raise ValueError("image_result_too_large")
    try:
        content = base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        raise ValueError("image_result_invalid_base64") from exc
    if not content or len(content) > maximum:
        raise ValueError("image_result_too_large")
    try:
        with Image.open(io.BytesIO(content)) as img:
            image_format = img.format
            pixels = img.width * img.height
            if image_format not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError("image_result_invalid_format")
            if pixels < 1 or pixels > settings.media_image_max_pixels:
                raise ValueError("image_result_invalid_dimensions")
            img.verify()
        with Image.open(io.BytesIO(content)) as img:
            img.load()
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("image_result_corrupt") from exc
    mime = Image.MIME[image_format]
    ext = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}[image_format]
    key = f"images/nano-banana-2.1/{generation.id}/{hashlib.sha256(content).hexdigest()}.{ext}"
    storage = get_media_storage()
    await storage.put(key=key, content=content, content_type=mime)
    asset = MediaAsset(
        generation_id=generation.id,
        partner_id=generation.partner_id,
        kind="image",
        status="stored",
        provider="argolink",
        provider_content_url="stored-image",
        storage_backend=settings.media_storage_backend,
        storage_key=key,
        public_url="pending",
        content_type=mime,
        byte_size=len(content),
    )
    db.add(asset)
    await db.flush()
    asset.public_url = build_partner_media_url(asset.id)
    generation.result_url = asset.public_url
    generation.request_payload = {
        **(generation.request_payload or {}),
        "result_urls": [asset.public_url],
    }
    await db.flush()
    return asset