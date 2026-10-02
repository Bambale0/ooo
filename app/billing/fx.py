"""Persisted RUB/USDT policy.

Normal pricing reads are network-free. Crypto Pay exchange rates are refreshed only
while a new invoice is being prepared, then stored for later pricing/credit use.
"""

from datetime import UTC
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import select

from app.billing.models import FxFallbackSetting, FxRateSnapshot
from app.infrastructure.config import get_settings
from app.infrastructure.retry import utc_now
from app.payments.crypto_pay import CryptoPayError, get_crypto_pay_client


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _valid_rate(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    try:
        rate = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HTTPException(422, "invalid_fx_rate") from exc
    if not rate.is_finite() or not Decimal(0) < rate < Decimal("1000000000000"):
        raise HTTPException(422, "invalid_fx_rate")
    return rate


async def _latest_policy_row(db) -> FxFallbackSetting | None:
    return (
        await db.execute(
            select(FxFallbackSetting)
            .order_by(FxFallbackSetting.created_at.desc(), FxFallbackSetting.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _latest_automatic_row(db) -> FxRateSnapshot | None:
    return (
        await db.execute(
            select(FxRateSnapshot)
            .order_by(FxRateSnapshot.created_at.desc(), FxRateSnapshot.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def fx_policy(db) -> dict:
    setting = await _latest_policy_row(db)
    if setting is None:
        return {
            "automatic_enabled": True,
            "manual_rate": get_settings().rub_per_usdt,
            "updated_at": None,
        }
    return {
        "automatic_enabled": bool(setting.automatic_enabled),
        "manual_rate": setting.rate,
        "updated_at": aware(setting.created_at).isoformat(),
    }


async def current_fx(db) -> dict:
    """Return persisted/configured FX without external I/O."""

    policy = await fx_policy(db)
    automatic = await _latest_automatic_row(db)
    automatic_at = aware(automatic.created_at).isoformat() if automatic else None

    if not policy["automatic_enabled"]:
        manual = _valid_rate(policy["manual_rate"])
        if manual is None:
            raise HTTPException(503, "provider_temporarily_unavailable")
        return {"rate": manual, "source": "manual", "automatic_at": automatic_at}

    if automatic is not None:
        return {"rate": automatic.rate, "source": "automatic", "automatic_at": automatic_at}

    fallback = _valid_rate(policy["manual_rate"])
    if fallback is not None:
        return {"rate": fallback, "source": "manual_fallback", "automatic_at": None}
    raise HTTPException(503, "provider_temporarily_unavailable")


async def refresh_fx_for_invoice(db, *, client=None) -> dict:
    """Refresh automatic FX once for invoice creation; manual mode is network-free."""

    policy = await fx_policy(db)
    if not policy["automatic_enabled"]:
        return await current_fx(db)

    provider = client or get_crypto_pay_client()
    try:
        rate = _valid_rate(await provider.get_rub_per_usdt())
    except (CryptoPayError, ValueError, TypeError, HTTPException):
        # Invoice creation remains available when the exchange-rate endpoint is
        # temporarily unavailable, but only from an already persisted/operator
        # fallback. No other runtime path retries the rate endpoint.
        return await current_fx(db)
    automatic = FxRateSnapshot(rate=rate, created_at=utc_now())
    db.add(automatic)
    await db.flush()
    return {
        "rate": rate,
        "source": "automatic",
        "automatic_at": aware(automatic.created_at).isoformat(),
    }


def snapshot(fx: dict) -> dict:
    return {**fx, "rate": str(fx["rate"])}


def restore_snapshot(value: dict | None) -> dict | None:
    if not isinstance(value, dict) or "rate" not in value:
        return None
    try:
        rate = _valid_rate(Decimal(str(value["rate"])))
    except (InvalidOperation, HTTPException) as exc:
        raise HTTPException(503, "invalid_fx_snapshot") from exc
    if rate is None:
        raise HTTPException(503, "invalid_fx_snapshot")
    return {**value, "rate": rate}


async def set_fx_policy(
    db,
    *,
    automatic_enabled: bool,
    rate: Decimal | None,
    actor: str,
    reason: str,
) -> None:
    manual = _valid_rate(rate)
    if not automatic_enabled and manual is None:
        raise HTTPException(422, "manual_fx_rate_required")
    db.add(
        FxFallbackSetting(
            rate=manual,
            automatic_enabled=automatic_enabled,
            actor=actor,
            reason=reason,
            created_at=utc_now(),
        )
    )
    await db.flush()


async def set_manual_fallback(db, *, rate: Decimal | None, actor: str, reason: str) -> None:
    """Backward-compatible operator fallback while automatic mode stays enabled."""

    await set_fx_policy(
        db,
        automatic_enabled=True,
        rate=rate,
        actor=actor,
        reason=reason,
    )
