from __future__ import annotations

import secrets
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.models import Partner
from app.referrals.models import ReferralAttribution, ReferralCommission, ReferralProfile

API_TOPUP_RATE = Decimal("0.07")
DEVELOPMENT_ORDER_RATE = Decimal("0.20")
RUB_CENT = Decimal("0.01")


def commission_amount(base_amount_rub: Decimal, rate: Decimal) -> Decimal:
    base = Decimal(base_amount_rub)
    if base <= 0:
        raise ValueError("commission base must be positive")
    return (base * Decimal(rate)).quantize(RUB_CENT, rounding=ROUND_HALF_UP)


async def get_or_create_profile(db: AsyncSession, partner_id: str) -> ReferralProfile:
    owner = await db.get(Partner, partner_id, with_for_update=True)
    if owner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="partner_not_found")
    profile = await db.scalar(select(ReferralProfile).where(ReferralProfile.partner_id == partner_id))
    if profile is not None:
        return profile
    profile = ReferralProfile(partner_id=partner_id, code=secrets.token_hex(8))
    db.add(profile)
    await db.flush()
    return profile


async def bind_referral(db: AsyncSession, *, referred_telegram_id: str, code: str | None) -> bool:
    """Persist the first bot visit; only its valid referral code may attribute."""
    existing = await db.scalar(
        select(ReferralAttribution).where(ReferralAttribution.referred_telegram_id == referred_telegram_id)
    )
    if existing is not None:
        return False
    profile = None
    owner = None
    if code and code.isalnum() and 8 <= len(code) <= 24:
        profile = await db.scalar(select(ReferralProfile).where(ReferralProfile.code == code))
        if profile is not None:
            owner = await db.get(Partner, profile.partner_id)
            if owner is None or owner.status != "active" or owner.telegram_id == referred_telegram_id:
                profile = None
                owner = None
    db.add(
        ReferralAttribution(
            referrer_partner_id=owner.id if owner else None,
            referred_partner_id=await db.scalar(select(Partner.id).where(Partner.telegram_id == referred_telegram_id)),
            referred_telegram_id=referred_telegram_id,
            referral_code=profile.code if profile else None,
        )
    )
    await db.flush()
    return owner is not None


async def claim_partner_attribution(db: AsyncSession, partner: Partner) -> ReferralAttribution | None:
    attribution = await db.scalar(
        select(ReferralAttribution).where(ReferralAttribution.referred_telegram_id == partner.telegram_id)
    )
    if attribution is not None and attribution.referred_partner_id is None:
        attribution.referred_partner_id = partner.id
        await db.flush()
    return attribution


async def accrue_commission(
    db: AsyncSession,
    *,
    referred_telegram_id: str,
    referred_partner_id: str | None = None,
    kind: str,
    source_id: str,
    event_key: str,
    base_amount_rub: Decimal,
    rate: Decimal,
    reverse: bool = False,
    amount_rub: Decimal | None = None,
) -> ReferralCommission | None:
    existing = await db.scalar(select(ReferralCommission).where(ReferralCommission.event_key == event_key))
    if existing is not None:
        return existing
    attribution = None
    if referred_partner_id is not None:
        attribution = await db.scalar(
            select(ReferralAttribution).where(ReferralAttribution.referred_partner_id == referred_partner_id)
        )
    if attribution is None:
        attribution = await db.scalar(
            select(ReferralAttribution).where(ReferralAttribution.referred_telegram_id == referred_telegram_id)
        )
        if attribution is not None and referred_partner_id is not None and attribution.referred_partner_id is None:
            attribution.referred_partner_id = referred_partner_id
    if attribution is None or attribution.referrer_partner_id is None:
        return None
    amount = Decimal(amount_rub) if amount_rub is not None else commission_amount(base_amount_rub, rate)
    amount = amount.quantize(RUB_CENT, rounding=ROUND_HALF_UP)
    if amount <= 0:
        raise ValueError("commission amount must be positive")
    if reverse:
        amount = -amount
    commission = ReferralCommission(
        referrer_partner_id=attribution.referrer_partner_id,
        referred_telegram_id=referred_telegram_id,
        kind=kind,
        event_key=event_key,
        source_id=source_id,
        base_amount_rub=Decimal(base_amount_rub),
        rate=Decimal(rate),
        amount_rub=amount,
    )
    db.add(commission)
    await db.flush()
    return commission


async def referral_stats(db: AsyncSession, partner_id: str) -> dict[str, Decimal | int]:
    invited = await db.scalar(
        select(func.count())
        .select_from(ReferralAttribution)
        .where(ReferralAttribution.referrer_partner_id == partner_id)
    )
    earned = await db.scalar(
        select(func.coalesce(func.sum(ReferralCommission.amount_rub), 0)).where(
            ReferralCommission.referrer_partner_id == partner_id
        )
    )
    return {"invited": int(invited or 0), "balance_rub": Decimal(earned or 0)}
