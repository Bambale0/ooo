"""Automatic -> configured manual fallback -> last automatic, with provenance."""

from datetime import UTC
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select

from app.billing.models import FxFallbackSetting, FxRateSnapshot
from app.infrastructure.config import get_settings
from app.infrastructure.retry import utc_now
from app.payments.crypto_pay import CryptoPayError, get_crypto_pay_client


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


async def current_fx(db) -> dict:
    automatic = (
        await db.execute(select(FxRateSnapshot).order_by(FxRateSnapshot.created_at.desc()).limit(1))
    ).scalar_one_or_none()
    last_success = aware(automatic.created_at) if automatic else None
    if automatic and (utc_now() - last_success).total_seconds() < 60:
        return {"rate": automatic.rate, "source": "automatic", "automatic_at": last_success.isoformat()}
    try:
        rate = await get_crypto_pay_client().get_rub_per_usdt()
    except (CryptoPayError, ValueError):
        manual = (
            await db.execute(select(FxFallbackSetting).order_by(FxFallbackSetting.created_at.desc()).limit(1))
        ).scalar_one_or_none()
        # The environment value is a bootstrap operator fallback. An explicit
        # disabled database setting takes precedence over that bootstrap value.
        rate = manual.rate if manual else get_settings().rub_per_usdt
        if rate is not None:
            return {
                "rate": rate,
                "source": "manual_fallback",
                "automatic_at": last_success.isoformat() if last_success else None,
            }
        if automatic:
            return {"rate": automatic.rate, "source": "last_automatic", "automatic_at": last_success.isoformat()}
        raise HTTPException(503, "provider_temporarily_unavailable") from None
    automatic = FxRateSnapshot(rate=rate, created_at=utc_now())
    db.add(automatic)
    await db.flush()
    return {"rate": rate, "source": "automatic", "automatic_at": aware(automatic.created_at).isoformat()}


def snapshot(fx: dict) -> dict:
    return {**fx, "rate": str(fx["rate"])}


async def set_manual_fallback(db, *, rate: Decimal | None, actor: str, reason: str) -> None:
    if rate is not None and (not rate.is_finite() or not Decimal(0) < rate < Decimal("1000000000000")):
        raise HTTPException(422, "invalid_fx_rate")
    db.add(FxFallbackSetting(rate=rate, actor=actor, reason=reason, created_at=utc_now()))
    await db.flush()
