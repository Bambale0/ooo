from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.config import get_settings
from app.infrastructure.security import decrypt_secret
from app.providers.base import ProviderAdapter
from app.providers.infai_video import INFAI_CREDENTIAL_LABEL
from app.providers.models import ProviderCredential
from app.providers.registry import get_provider_adapter


async def get_active_provider_credential(
    db: AsyncSession,
    partner_id: str,
    provider: str,
) -> ProviderCredential | None:
    scope = (
        (ProviderCredential.partner_id.is_(None) & (ProviderCredential.label == INFAI_CREDENTIAL_LABEL))
        if provider == "infai"
        else ProviderCredential.partner_id == partner_id
    )
    result = await db.execute(
        select(ProviderCredential)
        .where(
            scope,
            ProviderCredential.provider == provider,
            ProviderCredential.is_active.is_(True),
            ProviderCredential.encrypted_api_key.is_not(None),
        )
        .order_by(ProviderCredential.created_at.desc())
        .with_for_update()
    )
    return result.scalars().first()


async def has_provider_runtime_credential(
    db: AsyncSession,
    partner_id: str,
    provider: str,
) -> bool:
    if provider == "asale" and get_settings().asale_api_key:
        return True
    return await get_active_provider_credential(db, partner_id, provider) is not None


async def get_partner_provider_adapter(
    db: AsyncSession,
    partner_id: str,
    provider: str,
    *,
    credential_id: str | None = None,
) -> ProviderAdapter:
    if credential_id is None and provider == "asale" and get_settings().asale_api_key:
        return get_provider_adapter(provider, api_key=get_settings().asale_api_key)
    if credential_id is None:
        credential = await get_active_provider_credential(db, partner_id, provider)
    else:
        # Existing jobs belong to the original upstream account, even after key rotation.
        scope = (
            (ProviderCredential.partner_id.is_(None) & (ProviderCredential.label == INFAI_CREDENTIAL_LABEL))
            if provider == "infai"
            else ProviderCredential.partner_id == partner_id
        )
        result = await db.execute(
            select(ProviderCredential).where(
                ProviderCredential.id == credential_id,
                scope,
                ProviderCredential.provider == provider,
            )
        )
        credential = result.scalar_one_or_none()
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
