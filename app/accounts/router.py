from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update

from app.accounts.models import (
    ApiKey,
    ConsentAcceptance,
    Partner,
    PartnerAccountStateHistory,
    PartnerApplication,
)
from app.accounts.schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyRead,
    DeletePartnerCreate,
    PartnerApplicationCreate,
    PartnerApplicationRead,
    PartnerRead,
    RejectApplicationCreate,
)
from app.api.dependencies import DbSession, require_admin
from app.infrastructure.config import get_settings
from app.infrastructure.security import create_api_key, encrypt_secret
from app.providers.models import ProviderCredential

router = APIRouter()


@router.post("/applications", response_model=PartnerApplicationRead, status_code=status.HTTP_201_CREATED)
async def submit_application(payload: PartnerApplicationCreate, db: DbSession) -> PartnerApplication:
    if not payload.accepted_terms or not payload.accepted_privacy_policy:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="consent_required")

    existing = await db.execute(
        select(PartnerApplication).where(
            PartnerApplication.telegram_id == payload.telegram_id,
            PartnerApplication.status == "pending",
        )
    )
    pending = existing.scalar_one_or_none()
    if pending is not None:
        return pending

    application_data = payload.model_dump(
        exclude={"accepted_terms", "accepted_privacy_policy"}
    )
    application = PartnerApplication(**application_data)
    db.add(application)
    await db.flush()
    db.add_all(
        [
            ConsentAcceptance(
                telegram_id=application.telegram_id,
                partner_application_id=application.id,
                document_type="terms",
                document_version=application.terms_version,
            ),
            ConsentAcceptance(
                telegram_id=application.telegram_id,
                partner_application_id=application.id,
                document_type="privacy_policy",
                document_version=application.privacy_policy_version,
            ),
        ]
    )
    await db.refresh(application)
    return application


@router.get(
    "/applications",
    response_model=list[PartnerApplicationRead],
    dependencies=[Depends(require_admin)],
)
async def list_applications(db: DbSession) -> list[PartnerApplication]:
    result = await db.execute(select(PartnerApplication).order_by(PartnerApplication.created_at.desc()))
    return list(result.scalars().all())


@router.post(
    "/applications/{application_id}/approve",
    response_model=PartnerRead,
    dependencies=[Depends(require_admin)],
)
async def approve_application(application_id: str, db: DbSession) -> Partner:
    application = await db.get(PartnerApplication, application_id)
    if application is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="application_not_found")
    if application.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="application_already_decided")

    provider_key_result = await db.execute(
        select(ProviderCredential).where(
            ProviderCredential.provider == "argolink",
            ProviderCredential.partner_application_id == application.id,
            ProviderCredential.is_active.is_(True),
            ProviderCredential.encrypted_api_key.is_not(None),
        )
    )
    if provider_key_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="required_provider_key_missing")

    existing = await db.execute(
        select(Partner).where(Partner.telegram_id == application.telegram_id, Partner.status != "deleted")
    )
    partner = existing.scalar_one_or_none()
    if partner is None:
        partner = Partner(
            application_id=application.id,
            telegram_id=application.telegram_id,
            company_name=application.company_name,
            project_name=application.project_name,
        )
        db.add(partner)
        await db.flush()
        db.add(
            PartnerAccountStateHistory(
                partner_id=partner.id,
                from_status=None,
                to_status="active",
                reason="application_approved",
            )
        )
    application.status = "approved"
    await db.execute(
        update(ConsentAcceptance)
        .where(ConsentAcceptance.partner_application_id == application.id)
        .values(partner_id=partner.id)
    )
    await db.execute(
        update(ProviderCredential)
        .where(
            ProviderCredential.partner_application_id == application.id,
            ProviderCredential.is_active.is_(True),
        )
        .values(partner_id=partner.id)
    )
    await db.flush()
    await db.refresh(partner)
    return partner


@router.post(
    "/applications/{application_id}/reject",
    response_model=PartnerApplicationRead,
    dependencies=[Depends(require_admin)],
)
async def reject_application(
    application_id: str,
    payload: RejectApplicationCreate,
    db: DbSession,
) -> PartnerApplication:
    application = await db.get(PartnerApplication, application_id)
    if application is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="application_not_found")
    if application.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="application_already_decided")
    application.status = "rejected"
    application.rejection_reason = payload.reason
    await db.flush()
    await db.refresh(application)
    return application


@router.post(
    "/partners/{partner_id}/api-keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_partner_api_key(partner_id: str, payload: ApiKeyCreate, db: DbSession) -> ApiKeyCreated:
    partner = await db.get(Partner, partner_id)
    if partner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="partner_not_found")
    token, token_hash = create_api_key()
    webhook_secret_encrypted = None
    if payload.webhook_secret is not None:
        master_key = get_settings().webhook_secrets_master_key
        if not master_key:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="webhook_secret_encryption_not_configured",
            )
        webhook_secret_encrypted = encrypt_secret(payload.webhook_secret, master_key)

    api_key = ApiKey(
        partner_id=partner.id,
        name=payload.name,
        key_hash=token_hash,
        key_prefix=token[:8],
        webhook_url=str(payload.webhook_url) if payload.webhook_url is not None else None,
        webhook_secret_encrypted=webhook_secret_encrypted,
    )
    db.add(api_key)
    await db.flush()
    await db.refresh(api_key)
    return ApiKeyCreated(
        id=api_key.id,
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        api_key=token,
        webhook_url=api_key.webhook_url,
    )


@router.get(
    "/partners/{partner_id}/api-keys",
    response_model=list[ApiKeyRead],
    dependencies=[Depends(require_admin)],
)
async def list_partner_api_keys(partner_id: str, db: DbSession) -> list[ApiKey]:
    result = await db.execute(select(ApiKey).where(ApiKey.partner_id == partner_id))
    return list(result.scalars().all())


@router.post(
    "/partners/{partner_id}/api-keys/{api_key_id}/revoke",
    response_model=ApiKeyRead,
    dependencies=[Depends(require_admin)],
)
async def revoke_partner_api_key(partner_id: str, api_key_id: str, db: DbSession) -> ApiKey:
    api_key = await db.get(ApiKey, api_key_id)
    if api_key is None or api_key.partner_id != partner_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="api_key_not_found")
    api_key.is_active = False
    await db.flush()
    await db.refresh(api_key)
    return api_key


@router.post(
    "/partners/{partner_id}/delete",
    response_model=PartnerRead,
    dependencies=[Depends(require_admin)],
)
async def delete_partner(
    partner_id: str,
    payload: DeletePartnerCreate,
    db: DbSession,
) -> Partner:
    partner = await db.get(Partner, partner_id)
    if partner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="partner_not_found")
    if partner.status == "deleted":
        return partner
    if Decimal(partner.balance_rub) < Decimal("0.00"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="negative_balance_delete_forbidden")
    previous_status = partner.status
    partner.status = "deleted"
    db.add(
        PartnerAccountStateHistory(
            partner_id=partner.id,
            from_status=previous_status,
            to_status="deleted",
            reason=payload.reason,
        )
    )
    await db.execute(update(ApiKey).where(ApiKey.partner_id == partner.id).values(is_active=False))
    await db.flush()
    await db.refresh(partner)
    return partner
