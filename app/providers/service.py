from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.config import get_settings
from app.infrastructure.security import decrypt_secret
from app.providers.base import ProviderAdapter
from app.providers.models import ProviderCredential
from app.providers.registry import get_provider_adapter


async def get_active_provider_credential(
    db: AsyncSession,
    partner_id: str,
    provider: str,
) -> ProviderCredential | None:
    result = await db.execute(
        select(ProviderCredential)
        .where(
            ProviderCredential.partner_id == partner_id,
            ProviderCredential.provider == provider,
            ProviderCredential.is_active.is_(True),
            ProviderCredential.encrypted_api_key.is_not(None),
        )
        .order_by(ProviderCredential.created_at.desc())
    )
    return result.scalars().first()


async def get_partner_provider_adapter(
    db: AsyncSession,
    partner_id: str,
    provider: str,
) -> ProviderAdapter:
    credential = await get_active_provider_credential(db, partner_id, provider)
    if credential is None or not credential.encrypted_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="provider_temporarily_unavailable",
        )

    master_key = get_settings().provider_credentials_master_key
    if not master_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="provider_temporarily_unavailable",
        )
    try:
        api_key = decrypt_secret(credential.encrypted_api_key, master_key)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="provider_temporarily_unavailable",
        ) from exc
    return get_provider_adapter(provider, api_key=api_key)
