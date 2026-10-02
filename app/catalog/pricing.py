"""Partner price overrides: fixed RUB retail with current global procurement."""

from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.models import Partner
from app.catalog.models import Model, PartnerPrice, PartnerPriceHistory


def price_key(price: PartnerPrice) -> tuple[str, str, str]:
    return price.model_id, price.mode, price.resolution


async def resolve_partner_price(
    db: AsyncSession,
    *,
    model_id: str,
    mode: str,
    resolution: str,
    partner_id: str,
) -> tuple[PartnerPrice, PartnerPrice | None]:
    """Return (current global procurement row, optional fixed partner retail row)."""
    global_price = (
        await db.execute(
            select(PartnerPrice).where(
                PartnerPrice.model_id == model_id,
                PartnerPrice.partner_id.is_(None),
                PartnerPrice.mode == mode,
                PartnerPrice.resolution == resolution,
            )
        )
    ).scalar_one_or_none()
    if global_price is None:
        raise HTTPException(404, "model_or_price_not_available")
    override = (
        await db.execute(
            select(PartnerPrice).where(
                PartnerPrice.model_id == model_id,
                PartnerPrice.partner_id == partner_id,
                PartnerPrice.mode == mode,
                PartnerPrice.resolution == resolution,
            )
        )
    ).scalar_one_or_none()
    return global_price, override


def retail_price(global_price: PartnerPrice, override: PartnerPrice | None) -> Decimal:
    return Decimal(override.price_rub if override is not None else global_price.price_rub)


async def effective_price_pairs(
    db: AsyncSession,
    partner_id: str,
) -> list[tuple[PartnerPrice, PartnerPrice | None]]:
    """Production global prices paired with this partner's optional RUB overrides."""
    global_prices = list(
        (
            await db.execute(
                select(PartnerPrice)
                .join(Model, Model.id == PartnerPrice.model_id)
                .where(
                    Model.status == "production",
                    PartnerPrice.partner_id.is_(None),
                )
            )
        ).scalars()
    )
    overrides = list(
        (
            await db.execute(
                select(PartnerPrice).where(PartnerPrice.partner_id == partner_id)
            )
        ).scalars()
    )
    by_key = {price_key(row): row for row in overrides}
    return [(row, by_key.get(price_key(row))) for row in global_prices]


async def worst_cost_to_retail_ratio(
    db: AsyncSession,
    partner_id: str,
    *,
    fx: Decimal | None = None,
) -> Decimal:
    """Worst current procurement / effective retail ratio for one partner."""
    ratios: list[Decimal] = []
    for global_price, override in await effective_price_pairs(db, partner_id):
        retail = retail_price(global_price, override)
        cost = Decimal(global_price.provider_cost_usdt)
        if retail <= 0:
            continue
        ratio = cost / retail
        ratios.append(ratio * fx if fx is not None else ratio)
    return max(ratios, default=Decimal(1) if fx is not None else Decimal(0))


async def snapshot_global_prices_for_partner(
    db: AsyncSession,
    partner_id: str,
    *,
    actor: str = "admin_api",
) -> int:
    """Freeze current production RUB retail prices for an existing partner.

    Procurement is intentionally *not* frozen: admission and coverage always use
    the current global procurement row.
    """
    del actor  # Price history is the existing durable audit surface.
    from app.billing.fx import current_fx
    from app.billing.fx import snapshot as fx_snapshot

    partner = (
        await db.execute(select(Partner).where(Partner.id == partner_id).with_for_update())
    ).scalar_one_or_none()
    if partner is None:
        raise HTTPException(404, "partner_not_found")

    global_prices = list(
        (
            await db.execute(
                select(PartnerPrice)
                .join(Model, Model.id == PartnerPrice.model_id)
                .where(
                    Model.status == "production",
                    PartnerPrice.partner_id.is_(None),
                )
                .with_for_update()
            )
        ).scalars()
    )
    if not global_prices:
        return 0

    existing = list(
        (
            await db.execute(
                select(PartnerPrice).where(PartnerPrice.partner_id == partner_id)
            )
        ).scalars()
    )
    existing_keys = {price_key(row) for row in existing}
    fx_data = await current_fx(db)
    count = 0

    for global_price in global_prices:
        if price_key(global_price) in existing_keys:
            continue
        db.add(
            PartnerPrice(
                model_id=global_price.model_id,
                partner_id=partner_id,
                mode=global_price.mode,
                resolution=global_price.resolution,
                price_rub=global_price.price_rub,
                provider_cost_usdt=global_price.provider_cost_usdt,
                billing_unit=global_price.billing_unit,
            )
        )
        db.add(
            PartnerPriceHistory(
                model_id=global_price.model_id,
                partner_id=partner_id,
                mode=global_price.mode,
                resolution=global_price.resolution,
                old_price_rub=None,
                new_price_rub=global_price.price_rub,
                old_provider_cost_usdt=None,
                new_provider_cost_usdt=global_price.provider_cost_usdt,
                billing_unit=global_price.billing_unit,
                rub_per_usdt_snapshot=fx_data["rate"],
                fx_snapshot=fx_snapshot(fx_data),
            )
        )
        count += 1

    await db.flush()
    return count
