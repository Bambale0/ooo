"""Append-only threshold overrides; no pricing or admission side effects."""

from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select, text

from app.billing.models import MarginThresholdHistory
from app.catalog.models import Model, PartnerPrice


async def margin_scope(db, model=None, mode=None, resolution=None):
    if (mode is None) != (resolution is None) or (mode is not None and model is None):
        raise HTTPException(422, "invalid_threshold_scope")
    if model is None:
        return "global"
    row = (await db.execute(select(Model).where(Model.slug == model))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "model_not_found")
    if mode is None:
        return f"model:{row.id}"
    # Use global price (partner_id IS NULL) for margin scope resolution
    price = (
        await db.execute(
            select(PartnerPrice).where(
                PartnerPrice.model_id == row.id,
                PartnerPrice.partner_id.is_(None),
                PartnerPrice.mode == mode,
                PartnerPrice.resolution == resolution,
            )
        )
    ).scalar_one_or_none()
    if price is None:
        raise HTTPException(404, "price_not_found")
    return f"configuration:{price.id}"


async def overrides(db):
    rows = (
        await db.execute(
            select(MarginThresholdHistory).order_by(
                MarginThresholdHistory.created_at,
                MarginThresholdHistory.id,
            )
        )
    ).scalars()
    return {row.scope: row.new_value for row in rows}


def threshold(values, model_id, price_id):
    for key in (f"configuration:{price_id}", f"model:{model_id}", "global"):
        if values.get(key) is not None:
            return values[key]
    return Decimal(30)


async def set_threshold(db, *, actor, value, model=None, mode=None, resolution=None):
    if value is not None and (not value.is_finite() or not 0 <= value <= 100):
        raise HTTPException(422, "invalid_margin_threshold")
    if db.bind.dialect.name == "postgresql":
        await db.execute(text("SELECT pg_advisory_xact_lock(728346022)"))
    scope = await margin_scope(db, model, mode, resolution)
    values = await overrides(db)
    old = values.get(scope, Decimal(30) if scope == "global" else None)
    row = MarginThresholdHistory(scope=scope, old_value=old, new_value=value, actor=actor)
    db.add(row)
    await db.flush()
    return row
