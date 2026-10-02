from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update

from app.accounts.models import Partner, PartnerApplication
from app.api.dependencies import DbSession, require_admin
from app.catalog.models import Model
from app.infrastructure.config import get_settings
from app.infrastructure.security import encrypt_secret, hash_secret
from app.providers.models import ProviderCredential, ProviderModelCapability
from app.providers.registry import get_provider_adapter
from app.providers.schemas import (
    ProviderCapabilityRead,
    ProviderCapabilityUpsert,
    ProviderCredentialCreate,
    ProviderCredentialRead,
)

router = APIRouter()


@router.post(
    "/credentials",
    response_model=ProviderCredentialRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_provider_credential(
    payload: ProviderCredentialCreate,
    db: DbSession,
) -> ProviderCredential:
    settings = get_settings()
    if not settings.provider_credentials_master_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="provider_credentials_encryption_not_configured",
        )

    if payload.partner_application_id is not None:
        application = await db.get(PartnerApplication, payload.partner_application_id)
        if application is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="application_not_found")
        if application.status != "pending":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="application_not_pending")

    if payload.partner_id is not None:
        partner = await db.get(Partner, payload.partner_id)
        if partner is None or partner.status == "deleted":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="partner_not_found")

    adapter = get_provider_adapter(payload.provider, api_key=payload.api_key)
    if not await adapter.validate_key(payload.api_key):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="provider_key_invalid")

    owner_filters = []
    if payload.partner_application_id is not None:
        owner_filters.append(ProviderCredential.partner_application_id == payload.partner_application_id)
    if payload.partner_id is not None:
        owner_filters.append(ProviderCredential.partner_id == payload.partner_id)

    await db.execute(
        update(ProviderCredential)
        .where(
            ProviderCredential.provider == payload.provider,
            ProviderCredential.is_active.is_(True),
            *owner_filters,
        )
        .values(is_active=False)
    )

    credential = ProviderCredential(
        provider=payload.provider,
        label=payload.label,
        key_hash=hash_secret(payload.api_key),
        key_prefix=payload.api_key[:8],
        encrypted_api_key=encrypt_secret(payload.api_key, settings.provider_credentials_master_key),
        partner_application_id=payload.partner_application_id,
        partner_id=payload.partner_id,
    )
    db.add(credential)
    await db.flush()
    await db.refresh(credential)
    return credential


@router.get(
    "/credentials",
    response_model=list[ProviderCredentialRead],
    dependencies=[Depends(require_admin)],
)
async def list_provider_credentials(db: DbSession) -> list[ProviderCredential]:
    result = await db.execute(
        select(ProviderCredential).order_by(ProviderCredential.provider, ProviderCredential.created_at.desc())
    )
    return list(result.scalars().all())


@router.post(
    "/credentials/{credential_id}/revoke",
    response_model=ProviderCredentialRead,
    dependencies=[Depends(require_admin)],
)
async def revoke_provider_credential(credential_id: str, db: DbSession) -> ProviderCredential:
    credential = await db.get(ProviderCredential, credential_id)
    if credential is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider_credential_not_found")
    credential.is_active = False
    await db.flush()
    await db.refresh(credential)
    return credential


@router.put(
    "/capabilities",
    response_model=ProviderCapabilityRead,
    dependencies=[Depends(require_admin)],
)
async def upsert_provider_capability(
    payload: ProviderCapabilityUpsert,
    db: DbSession,
) -> ProviderModelCapability:
    model_result = await db.execute(select(Model).where(Model.slug == payload.model_slug))
    model = model_result.scalar_one_or_none()
    if model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="model_not_found")

    capability_result = await db.execute(
        select(ProviderModelCapability).where(
            ProviderModelCapability.provider == payload.provider,
            ProviderModelCapability.model_id == model.id,
            ProviderModelCapability.mode == payload.mode,
            ProviderModelCapability.resolution == payload.resolution,
        )
    )
    capability = capability_result.scalar_one_or_none()
    if capability is None:
        capability = ProviderModelCapability(
            provider=payload.provider,
            model_id=model.id,
            mode=payload.mode,
            resolution=payload.resolution,
            is_active=payload.is_active,
            provider_cost_ceiling_usdt=payload.provider_cost_ceiling_usdt,
            billing_unit=payload.billing_unit,
        )
        db.add(capability)
    else:
        capability.is_active = payload.is_active
        capability.provider_cost_ceiling_usdt = payload.provider_cost_ceiling_usdt
        capability.billing_unit = payload.billing_unit
    await db.flush()
    await db.refresh(capability)
    return capability
