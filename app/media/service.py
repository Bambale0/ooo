from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.infrastructure.retry import next_retry_at
from app.media.models import MediaAsset
from app.media.storage import get_media_storage
from app.providers.registry import get_provider_adapter


def build_partner_media_url(asset_id: str) -> str:
    settings = get_settings()
    if settings.public_media_base_url:
        return f"{settings.public_media_base_url.rstrip('/')}/{asset_id}.mp4"
    return f"{settings.public_api_base_url.rstrip('/')}{settings.api_prefix}/media/{asset_id}/content"


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
        generation.result_url = existing.public_url
        return existing

    asset = MediaAsset(
        generation_id=generation.id,
        partner_id=generation.partner_id,
        provider=provider,
        provider_content_url=provider_content_url,
        storage_backend="pending_ingest",
        public_url="pending",
        content_type="video/mp4",
    )
    db.add(asset)
    await db.flush()
    asset.public_url = build_partner_media_url(asset.id)
    generation.result_url = asset.public_url
    await db.flush()
    await db.refresh(asset)
    return asset


async def ingest_provider_asset(
    *,
    db: AsyncSession,
    asset: MediaAsset,
) -> MediaAsset:
    if asset.status == "stored":
        return asset

    settings = get_settings()
    adapter = get_provider_adapter(asset.provider)
    content, content_type = await adapter.fetch_result_content(asset.provider_content_url)
    if len(content) > settings.media_max_download_bytes:
        asset.status = "ingest_failed"
        asset.last_error = "media_asset_too_large"
        await db.flush()
        return asset

    media_content_type = content_type or asset.content_type or "video/mp4"
    storage_key = f"generations/{asset.generation_id}/{asset.id}.mp4"
    storage = get_media_storage()
    stored_key = await storage.put(
        key=storage_key,
        content=content,
        content_type=media_content_type,
    )
    asset.storage_backend = settings.media_storage_backend
    asset.storage_key = stored_key
    asset.content_type = media_content_type
    asset.byte_size = len(content)
    asset.status = "stored"
    asset.next_attempt_at = None
    asset.last_error = None
    await db.flush()
    await db.refresh(asset)
    return asset


def mark_ingest_retry(asset: MediaAsset, error: Exception) -> None:
    settings = get_settings()
    asset.last_error = type(error).__name__
    if asset.retry_count < settings.worker_max_retries:
        asset.retry_count += 1
        asset.status = "provider_ready"
        asset.next_attempt_at = next_retry_at(
            asset.retry_count,
            base_seconds=settings.worker_retry_base_seconds,
            max_seconds=settings.worker_retry_max_seconds,
        )
        return
    asset.status = "ingest_failed"
    asset.next_attempt_at = None
