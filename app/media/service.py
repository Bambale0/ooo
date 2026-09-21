from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.media.models import MediaAsset


def build_partner_media_url(asset_id: str) -> str:
    settings = get_settings()
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
