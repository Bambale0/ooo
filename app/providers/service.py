from fastapi import HTTPException, status
from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.models import Partner
from app.infrastructure.config import get_settings
from app.infrastructure.security import decrypt_secret
from app.providers.base import ProviderAdapter
from app.providers.infai_video import INFAI_CREDENTIAL_LABEL
from app.providers.models import PartnerProviderCredentialBinding, ProviderCredential
from app.providers.registry import get_provider_adapter


def partner_credential_scope(partner_id: str, provider: str, *, active_binding: bool = True):
    """Keep shared credentials private to explicitly bound tenants.

    Historical bindings remain usable for polling an already pinned attempt;
    an inactive binding cannot authorize a new submission.
    """
    if provider == "infai":
        return ProviderCredential.partner_id.is_(None) & (ProviderCredential.label == INFAI_CREDENTIAL_LABEL)
    binding = select(PartnerProviderCredentialBinding.partner_id).where(
        PartnerProviderCredentialBinding.partner_id == partner_id,
        PartnerProviderCredentialBinding.credential_id == ProviderCredential.id,
    )
    if active_binding:
        binding = binding.where(PartnerProviderCredentialBinding.is_active.is_(True))
    shared = (
        ProviderCredential.partner_id.is_(None)
        & ProviderCredential.partner_application_id.is_(None)
        & binding.correlate(ProviderCredential).exists()
    )
    return (ProviderCredential.partner_id == partner_id) | shared


async def get_active_provider_credential(
    db: AsyncSession,
    partner_id: str,
    provider: str,
    *,
    for_update: bool = True,
) -> ProviderCredential | None:
    scope = partner_credential_scope(partner_id, provider)
    query = (
        select(ProviderCredential)
        .where(
            scope,
            ProviderCredential.provider == provider,
            ProviderCredential.is_active.is_(True),
            ProviderCredential.encrypted_api_key.is_not(None),
        )
        .order_by(
            case((ProviderCredential.partner_id == partner_id, 0), else_=1),
            ProviderCredential.created_at.desc(),
            ProviderCredential.id.desc(),
        )
    )
    if for_update:
        query = query.with_for_update()
    result = await db.execute(query)
    return result.scalars().first()


async def has_provider_runtime_credential(
    db: AsyncSession,
    partner_id: str,
    provider: str,
    *,
    for_update: bool = True,
) -> bool:
    settings = get_settings()
    credential = await get_active_provider_credential(db, partner_id, provider, for_update=for_update)
    if credential is not None:
        return True
    if provider == "asale" and settings.asale_api_key:
        return True
    if provider == "argolink" and settings.argolink_shared_fallback_enabled and settings.argolink_api_key:
        partner = await db.get(Partner, partner_id)
        return bool(partner and not partner.requires_dedicated_argolink_credential)
    return False


async def get_partner_provider_adapter(
    db: AsyncSession,
    partner_id: str,
    provider: str,
    *,
    credential_id: str | None = None,
) -> ProviderAdapter:
    settings = get_settings()
    if credential_id is None:
        credential = await get_active_provider_credential(db, partner_id, provider)
        if credential is None and provider == "asale" and settings.asale_api_key:
            return get_provider_adapter(provider, api_key=settings.asale_api_key)
        if (
            credential is None
            and provider == "argolink"
            and settings.argolink_shared_fallback_enabled
            and settings.argolink_api_key
        ):
            partner = await db.get(Partner, partner_id)
            if partner is not None and not partner.requires_dedicated_argolink_credential:
                return get_provider_adapter(provider, api_key=settings.argolink_api_key)
    else:
        # Existing jobs belong to the original upstream account, even after key rotation.
        scope = partner_credential_scope(partner_id, provider, active_binding=False)
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
