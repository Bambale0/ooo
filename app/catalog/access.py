"""Per-partner access to restricted models; no public catalog visibility.

A `restricted` model is intentionally absent from /models, /pricing and the public
documentation. Every read of access goes through `has_model_grant`, so a partner can
use the model only while an administrator keeps an unrevoked grant for them.
"""

from fastapi import HTTPException
from sqlalchemy import select

from app.catalog.models import Model, PartnerModelGrant

RESTRICTED_STATUS = "restricted"


async def has_model_grant(db, model_id: str, partner_id: str) -> bool:
    """True only for a live grant. Revoked rows never count as access."""
    result = await db.execute(
        select(PartnerModelGrant.id).where(
            PartnerModelGrant.model_id == model_id,
            PartnerModelGrant.partner_id == partner_id,
            PartnerModelGrant.revoked_at.is_(None),
        )
    )
    return result.scalar_one_or_none() is not None


async def _restricted_model(db, model_slug: str) -> Model:
    model = (await db.execute(select(Model).where(Model.slug == model_slug))).scalar_one_or_none()
    if model is None:
        raise HTTPException(404, "model_not_found")
    if model.status != RESTRICTED_STATUS:
        raise HTTPException(409, "model_not_restricted")
    return model


async def grant_model_access(db, *, model_slug: str, partner_id: str, reason: str, actor: str) -> PartnerModelGrant:
    """Grant or restore access. Re-granting a revoked row keeps one history line."""
    from app.accounts.models import Partner

    model = await _restricted_model(db, model_slug)
    partner = (
        await db.execute(select(Partner).where(Partner.id == partner_id).with_for_update())
    ).scalar_one_or_none()
    if partner is None:
        raise HTTPException(404, "partner_not_found")
    existing = (
        await db.execute(
            select(PartnerModelGrant)
            .where(
                PartnerModelGrant.model_id == model.id,
                PartnerModelGrant.partner_id == partner_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.revoked_at = None
        existing.reason = reason
        existing.granted_by = actor
        await db.flush()
        return existing
    grant = PartnerModelGrant(model_id=model.id, partner_id=partner_id, granted_by=actor, reason=reason)
    db.add(grant)
    await db.flush()
    await db.refresh(grant)
    return grant


async def revoke_model_access(db, *, model_slug: str, partner_id: str, reason: str, actor: str) -> PartnerModelGrant:
    """Revoke access without deleting history; a revoked grant stops working at once."""
    from app.infrastructure.retry import utc_now

    model = await _restricted_model(db, model_slug)
    grant = (
        await db.execute(
            select(PartnerModelGrant)
            .where(
                PartnerModelGrant.model_id == model.id,
                PartnerModelGrant.partner_id == partner_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if grant is None or grant.revoked_at is not None:
        raise HTTPException(404, "grant_not_found")
    grant.revoked_at = utc_now()
    grant.reason = f"{reason} (revoked by {actor})"
    await db.flush()
    return grant
