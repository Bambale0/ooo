import hashlib
import hmac
import time
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.media.models import MediaAsset


def build_partner_media_url(asset_id: str) -> str:
    settings = get_settings()
    return f"{settings.public_api_base_url.rstrip('/')}{settings.api_prefix}/media/{asset_id}/content"


def is_internal_partner_media_url(value: str | None) -> bool:
    if not value:
        return False
    settings = get_settings()
    expected = urlsplit(settings.public_api_base_url.rstrip("/"))
    parsed = urlsplit(value)
    prefix = settings.api_prefix.rstrip("/") + "/media/"
    return (
        parsed.scheme == expected.scheme
        and parsed.netloc == expected.netloc
        and parsed.path.startswith(prefix)
        and parsed.path.endswith("/content")
        and not parsed.query
        and not parsed.fragment
    )


def result_download_signature(generation_id: str, partner_id: str, expires: int) -> str:
    key = get_settings().provider_credentials_master_key
    if not key:
        raise HTTPException(503, "content_not_available")
    message = f"result:{generation_id}:{partner_id}:{expires}".encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


def build_result_download_url(generation: Generation, *, now: int | None = None) -> tuple[str, int]:
    settings = get_settings()
    expires = (int(time.time()) if now is None else now) + settings.media_share_link_ttl_seconds
    token = result_download_signature(generation.id, generation.partner_id, expires)
    url = (
        f"{settings.public_api_base_url.rstrip('/')}{settings.api_prefix}/media/results/"
        f"{generation.id}/{expires}/{token}"
    )
    return url, expires


async def create_provider_ready_asset(
    *,
    db: AsyncSession,
    generation: Generation,
    provider: str,
    provider_content_url: str,
) -> MediaAsset:
    existing_result = await db.execute(
        select(MediaAsset).where(MediaAsset.generation_id == generation.id)
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        existing.provider_content_url = provider_content_url
        existing.status = "provider_ready"
        existing.storage_backend = None
        existing.storage_key = None
        existing.byte_size = None
        existing.last_error = None
        existing.next_attempt_at = None
        generation.result_url = existing.public_url
        await db.flush()
        return existing

    asset = MediaAsset(
        generation_id=generation.id,
        partner_id=generation.partner_id,
        provider=provider,
        provider_content_url=provider_content_url,
        storage_backend=None,
        storage_key=None,
        public_url="pending",
        content_type="video/mp4",
        byte_size=None,
        status="provider_ready",
    )
    db.add(asset)
    await db.flush()
    asset.public_url = build_partner_media_url(asset.id)
    generation.result_url = asset.public_url
    await db.flush()
    await db.refresh(asset)
    return asset
