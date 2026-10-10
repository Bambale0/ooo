from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog.models import PartnerPrice, PartnerPriceSnapshot


@dataclass(frozen=True)
class EffectivePartnerPrice:
    id: str
    model_id: str
    mode: str
    resolution: str
    price_rub: Decimal
    provider_cost_usdt: Decimal
    billing_unit: str


async def snapshot_partner_prices(db: AsyncSession, partner_id: str) -> int:
    existing = set(
        (
            await db.execute(
                select(PartnerPriceSnapshot.partner_price_id).where(
                    PartnerPriceSnapshot.partner_id == partner_id
                )
            )
        ).scalars()
    )
    templates = list((await db.execute(select(PartnerPrice))).scalars())
    created = 0
    for template in templates:
        if template.id in existing:
            continue
        db.add(
            PartnerPriceSnapshot(
                partner_id=partner_id,
                partner_price_id=template.id,
                price_rub=template.price_rub,
            )
        )
        created += 1
    if created:
        await db.flush()
    return created


async def effective_partner_prices(
    db: AsyncSession,
    *,
    partner_id: str,
    model_id: str,
) -> list[EffectivePartnerPrice]:
    await _snapshot_new_variants_for_partner(db, partner_id=partner_id, model_id=model_id)
    rows = (
        await db.execute(
            select(PartnerPrice, PartnerPriceSnapshot)
            .join(
                PartnerPriceSnapshot,
                PartnerPriceSnapshot.partner_price_id == PartnerPrice.id,
            )
            .where(
                PartnerPriceSnapshot.partner_id == partner_id,
                PartnerPrice.model_id == model_id,
            )
        )
    ).all()
    return [
        EffectivePartnerPrice(
            id=template.id,
            model_id=template.model_id,
            mode=template.mode,
            resolution=template.resolution,
            price_rub=snapshot.price_rub,
            provider_cost_usdt=template.provider_cost_usdt,
            billing_unit=template.billing_unit,
        )
        for template, snapshot in rows
    ]


async def effective_partner_price(
    db: AsyncSession,
    *,
    partner_id: str,
    model_id: str,
    mode: str,
    resolution: str,
) -> EffectivePartnerPrice | None:
    rows = await effective_partner_prices(db, partner_id=partner_id, model_id=model_id)
    return next(
        (row for row in rows if row.mode == mode and row.resolution == resolution),
        None,
    )


async def snapshot_price_for_existing_partners(
    db: AsyncSession,
    template: PartnerPrice,
) -> int:
    from app.accounts.models import Partner

    partner_ids = list(
        (
            await db.execute(
                select(Partner.id).where(Partner.status != "deleted")
            )
        ).scalars()
    )
    existing = set(
        (
            await db.execute(
                select(PartnerPriceSnapshot.partner_id).where(
                    PartnerPriceSnapshot.partner_price_id == template.id
                )
            )
        ).scalars()
    )
    created = 0
    for partner_id in partner_ids:
        if partner_id in existing:
            continue
        db.add(
            PartnerPriceSnapshot(
                partner_id=partner_id,
                partner_price_id=template.id,
                price_rub=template.price_rub,
            )
        )
        created += 1
    if created:
        await db.flush()
    return created


async def publish_global_partner_price(
    db: AsyncSession,
    template: PartnerPrice,
    price_rub: Decimal,
) -> int:
    """Publish the global price only to non-custom partner rates.

    Partner-specific rates are immutable with respect to later global price
    publications. Accepted and in-flight generation prices never change.
    """
    snapshots = list(
        (
            await db.execute(
                select(PartnerPriceSnapshot)
                .where(
                    PartnerPriceSnapshot.partner_price_id == template.id,
                    PartnerPriceSnapshot.is_custom.is_(False),
                )
                .with_for_update()
            )
        ).scalars()
    )
    template.price_rub = Decimal(price_rub)
    for snapshot in snapshots:
        snapshot.price_rub = Decimal(price_rub)
    await db.flush()
    return len(snapshots)


async def _snapshot_new_variants_for_partner(
    db: AsyncSession,
    *,
    partner_id: str,
    model_id: str,
) -> int:
    from app.accounts.models import Partner

    partner = await db.get(Partner, partner_id)
    if partner is None:
        return 0
    existing = set(
        (
            await db.execute(
                select(PartnerPriceSnapshot.partner_price_id).where(
                    PartnerPriceSnapshot.partner_id == partner_id
                )
            )
        ).scalars()
    )
    templates = list(
        (
            await db.execute(
                select(PartnerPrice).where(PartnerPrice.model_id == model_id)
            )
        ).scalars()
    )
    created = 0
    for template in templates:
        if template.id in existing:
            continue
        # Safe lazy enrollment is only for a pricing variant introduced after
        # this partner existed. Missing snapshots for older variants are a
        # rollout/data-integrity error and must stay fail-closed.
        if template.created_at < partner.created_at:
            continue
        db.add(
            PartnerPriceSnapshot(
                partner_id=partner_id,
                partner_price_id=template.id,
                price_rub=template.price_rub,
            )
        )
        created += 1
    if created:
        await db.flush()
    return created
