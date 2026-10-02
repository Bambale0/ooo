"""Partner-specific pricing utilities and 35% margin calculation."""

from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.models import Partner
from app.catalog.models import PartnerPrice, PartnerPriceHistory


def calculate_new_partner_price(provider_cost_usdt: Decimal, fx_rate: Decimal) -> Decimal:
    """Calculate price for new partners with 35% gross margin.

    Formula: price_rub = (provider_cost_usdt * fx_rate) / 0.65
    This gives 35% gross margin, which is approximately 53.85% markup.
    """
    cost_rub = provider_cost_usdt * fx_rate
    price_rub = (cost_rub / Decimal("0.65")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return price_rub


async def snapshot_global_prices_for_partner(
    db: AsyncSession, partner_id: str, *, actor: str = "admin_api"
) -> int:
    """Snapshot all current global prices as partner-specific prices.

    This freezes current pricing for an existing partner so that future global
    price changes won't affect them. Returns count of prices created.
    """
    from app.billing.fx import current_fx
    from app.billing.fx import snapshot as fx_snapshot

    # Validate partner exists
    partner = (await db.execute(select(Partner).where(Partner.id == partner_id))).scalar_one_or_none()
    if partner is None:
        raise HTTPException(404, "partner_not_found")

    # Get all global prices (partner_id IS NULL)
    global_prices = (
        await db.execute(select(PartnerPrice).where(PartnerPrice.partner_id.is_(None)))
    ).scalars().all()

    if not global_prices:
        return 0

    fx_data = await current_fx(db)
    rub_per_usdt = fx_data["rate"]
    count = 0

    for global_price in global_prices:
        # Check if partner-specific price already exists
        existing = (
            await db.execute(
                select(PartnerPrice).where(
                    PartnerPrice.model_id == global_price.model_id,
                    PartnerPrice.partner_id == partner_id,
                    PartnerPrice.mode == global_price.mode,
                    PartnerPrice.resolution == global_price.resolution,
                )
            )
        ).scalar_one_or_none()

        if existing is not None:
            continue  # Skip if already exists

        # Create partner-specific price
        partner_price = PartnerPrice(
            model_id=global_price.model_id,
            partner_id=partner_id,
            mode=global_price.mode,
            resolution=global_price.resolution,
            price_rub=global_price.price_rub,
            provider_cost_usdt=global_price.provider_cost_usdt,
            billing_unit=global_price.billing_unit,
        )
        db.add(partner_price)

        # Record in history
        history = PartnerPriceHistory(
            model_id=global_price.model_id,
            partner_id=partner_id,
            mode=global_price.mode,
            resolution=global_price.resolution,
            old_price_rub=None,
            new_price_rub=global_price.price_rub,
            old_provider_cost_usdt=None,
            new_provider_cost_usdt=global_price.provider_cost_usdt,
            billing_unit=global_price.billing_unit,
            rub_per_usdt_snapshot=rub_per_usdt,
            fx_snapshot=fx_snapshot(fx_data),
        )
        db.add(history)
        count += 1

    await db.flush()
    return count


async def create_new_partner_prices_with_margin(
    db: AsyncSession, partner_id: str, *, actor: str = "admin_api"
) -> int:
    """Create partner-specific prices for a new partner with 35% gross margin.

    Uses global prices as the base and applies the new partner margin formula.
    Returns count of prices created.
    """
    from app.billing.fx import current_fx
    from app.billing.fx import snapshot as fx_snapshot

    # Validate partner exists
    partner = (await db.execute(select(Partner).where(Partner.id == partner_id))).scalar_one_or_none()
    if partner is None:
        raise HTTPException(404, "partner_not_found")

    # Get all global prices
    global_prices = (
        await db.execute(select(PartnerPrice).where(PartnerPrice.partner_id.is_(None)))
    ).scalars().all()

    if not global_prices:
        return 0

    fx_data = await current_fx(db)
    rub_per_usdt = fx_data["rate"]
    count = 0

    for global_price in global_prices:
        # Check if partner-specific price already exists
        existing = (
            await db.execute(
                select(PartnerPrice).where(
                    PartnerPrice.model_id == global_price.model_id,
                    PartnerPrice.partner_id == partner_id,
                    PartnerPrice.mode == global_price.mode,
                    PartnerPrice.resolution == global_price.resolution,
                )
            )
        ).scalar_one_or_none()

        if existing is not None:
            continue  # Skip if already exists

        # Calculate new partner price with 35% margin
        new_price_rub = calculate_new_partner_price(global_price.provider_cost_usdt, rub_per_usdt)

        # Create partner-specific price
        partner_price = PartnerPrice(
            model_id=global_price.model_id,
            partner_id=partner_id,
            mode=global_price.mode,
            resolution=global_price.resolution,
            price_rub=new_price_rub,
            provider_cost_usdt=global_price.provider_cost_usdt,
            billing_unit=global_price.billing_unit,
        )
        db.add(partner_price)

        # Record in history
        history = PartnerPriceHistory(
            model_id=global_price.model_id,
            partner_id=partner_id,
            mode=global_price.mode,
            resolution=global_price.resolution,
            old_price_rub=None,
            new_price_rub=new_price_rub,
            old_provider_cost_usdt=None,
            new_provider_cost_usdt=global_price.provider_cost_usdt,
            billing_unit=global_price.billing_unit,
            rub_per_usdt_snapshot=rub_per_usdt,
            fx_snapshot=fx_snapshot(fx_data),
        )
        db.add(history)
        count += 1

    await db.flush()
    return count
