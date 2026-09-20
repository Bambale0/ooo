from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.api.dependencies import DbSession, require_admin
from app.catalog.models import Model
from app.infrastructure.security import hash_secret
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
    adapter = get_provider_adapter(payload.provider)
    if not await adapter.validate_key(payload.api_key):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="provider_key_invalid")
    existing_result = await db.execute(
        select(ProviderCredential).where(
            ProviderCredential.provider == payload.provider,
            ProviderCredential.is_active.is_(True),
        )
    )
    for existing in existing_result.scalars().all():
        existing.is_active = False
    credential = ProviderCredential(
        provider=payload.provider,
        label=payload.label,
        key_hash=hash_secret(payload.api_key),
        key_prefix=payload.api_key[:8],
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
    result = await db.execute(select(ProviderCredential).order_by(ProviderCredential.provider))
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
        )
        db.add(capability)
    else:
        capability.is_active = payload.is_active
    await db.flush()
    await db.refresh(capability)
    return capability
