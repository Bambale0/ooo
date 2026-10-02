"""Current cash cover is independent of historical partner coverage snapshots."""

from datetime import UTC
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import case, func, select, text

from app.accounts.models import Partner, ProfitWithdrawal
from app.billing.models import WalletSnapshot
from app.catalog.models import Model, PartnerPrice
from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.infrastructure.retry import utc_now
from app.payments.crypto_pay import CryptoPayError, get_crypto_pay_client
from app.payments.models import PaymentInvoice
from app.providers.base import ProviderAdapterError
from app.providers.service import get_partner_provider_adapter

ACTIVE = ("queued", "sent_to_provider", "processing", "timeout", "submitting", "reconciliation_required")


async def lock_capital(db):
    if db.bind.dialect.name == "postgresql":
        await db.execute(text("SELECT pg_advisory_xact_lock(728346019)"))


async def wallet_balance(db):
    latest = (
        await db.execute(select(WalletSnapshot).order_by(WalletSnapshot.created_at.desc()).limit(1))
    ).scalar_one_or_none()
    age = (
        None
        if latest is None
        else (
            utc_now()
            - (latest.created_at.astimezone(UTC) if latest.created_at.tzinfo else latest.created_at.replace(tzinfo=UTC))
        ).total_seconds()
    )
    if age is not None and age < get_settings().wallet_refresh_seconds:
        return latest.available_usdt, age, "fresh"
    try:
        balance = await get_crypto_pay_client().get_available_usdt()
    except (CryptoPayError, ValueError):
        return (latest.available_usdt, age, "stale") if latest else (None, None, "unavailable")
    db.add(WalletSnapshot(available_usdt=balance))
    await db.flush()
    return balance, 0, "fresh"


def paid_usdt(invoice):
    if invoice.paid_amount is None:
        return None
    if invoice.paid_asset == "USDT":
        amount = invoice.paid_amount
    elif invoice.paid_usd_rate is not None:
        amount = invoice.paid_amount * invoice.paid_usd_rate
    else:
        return None
    if invoice.requested_rub <= 0:
        return None
    return amount * (Decimal(1) - invoice.refunded_rub / invoice.requested_rub)


async def capital_state(db):
    settings = get_settings()
    wallet, age, freshness = await wallet_balance(db)
    gross = case(
        (PaymentInvoice.paid_asset == "USDT", PaymentInvoice.paid_amount),
        else_=PaymentInvoice.paid_amount * PaymentInvoice.paid_usd_rate,
    )
    net = case(
        (
            PaymentInvoice.requested_rub > 0,
            gross * (Decimal(1) - PaymentInvoice.refunded_rub / PaymentInvoice.requested_rub),
        ),
        else_=None,
    )
    received, pending, missing = (
        await db.execute(
            select(
                func.coalesce(func.sum(net), 0),
                func.coalesce(
                    func.sum(case((PaymentInvoice.status == "paid_waiting_credit", net), else_=Decimal(0))), 0
                ),
                func.count(case((net.is_(None), 1))),
            ).where(PaymentInvoice.paid_at.is_not(None))
        )
    ).one()
    if missing:
        return {"available": None, "safe": None, "freshness": "unreconciled_payments", "wallet_age_seconds": age}
    received, pending = Decimal(received), Decimal(pending)
    completed_cost = Decimal(
        (
            await db.execute(
                select(
                    func.coalesce(
                        func.sum(
                            func.coalesce(Generation.actual_provider_cost_usdt, Generation.provider_cost_usdt_snapshot)
                        ),
                        0,
                    )
                ).where(Generation.status == "completed")
            )
        ).scalar()
    )
    withdrawals = Decimal((await db.execute(select(func.coalesce(func.sum(ProfitWithdrawal.amount_usdt), 0)))).scalar())
    active_reserve = func.coalesce(
        Generation.provider_cost_reserve_usdt,
        Generation.provider_cost_usdt_snapshot,
    )
    active = Decimal(
        (
            await db.execute(
                select(func.coalesce(func.sum(active_reserve), 0)).where(
                    Generation.status.in_(ACTIVE)
                )
            )
        ).scalar()
    )
    rows = (
        (
            await db.execute(
                select(PartnerPrice)
                .join(Model, Model.id == PartnerPrice.model_id)
                .where(Model.status == "production", PartnerPrice.price_rub > 0)
            )
        )
        .scalars()
        .all()
    )
    ratio = max((p.provider_cost_usdt / p.price_rub for p in rows), default=None)
    balances = Decimal(
        (
            await db.execute(
                select(func.coalesce(func.sum(Partner.balance_rub), 0)).where(
                    Partner.balance_rub > 0, Partner.status != "deleted"
                )
            )
        ).scalar()
    )
    historical = Decimal((await db.execute(select(func.coalesce(func.sum(Partner.cost_coverage_rub), 0)))).scalar())
    future = balances * ratio if ratio is not None else Decimal(0) if balances == 0 else None
    book_cash = settings.opening_working_capital_usdt + received - completed_cost - withdrawals
    liquid = min(wallet, book_cash) if wallet is not None else None
    available = liquid - active - pending - settings.required_provider_float_usdt if liquid is not None else None
    safe = available - future if available is not None and future is not None else None
    return {
        "available": available,
        "safe": safe,
        "freshness": freshness,
        "wallet_age_seconds": age,
        "components": {
            "accessible_wallet_usdt": wallet,
            "book_working_capital_usdt": book_cash,
            "current_future_cost_reserve_usdt": future,
            "active_generation_reserve_usdt": active,
            "paid_pending_credit_usdt": pending,
            "required_provider_float_usdt": settings.required_provider_float_usdt,
            "historical_coverage_rub": historical,
            "partner_balance_rub": balances,
            "recorded_withdrawals_usdt": withdrawals,
        },
    }


async def require_current_capital(db, cost_usdt):
    await lock_capital(db)
    state = await capital_state(db)
    if state["available"] is None or state["available"] < cost_usdt or state["freshness"] != "fresh":
        raise HTTPException(503, "provider_temporarily_unavailable")


async def provider_capital_state(db, partner_id, provider="argolink"):
    """Spendable prepaid funds, never added to withdrawable cash or partner credit.

    Global reservations deliberately over-reserve when keys use separate wallets:
    the provider does not expose a reliable account identifier for pooling keys.
    Callers admitting new work must hold lock_capital through reservation commit.
    """
    adapter = await get_partner_provider_adapter(db, partner_id, provider)
    try:
        balance = await adapter.prepaid_balance_usdt()
    except ProviderAdapterError as exc:
        raise HTTPException(503, "provider_temporarily_unavailable") from exc
    active_reserve = func.coalesce(
        Generation.provider_cost_reserve_usdt,
        Generation.provider_cost_usdt_snapshot,
    )
    active = Decimal(
        (
            await db.execute(
                select(func.coalesce(func.sum(active_reserve), 0)).where(
                    Generation.status.in_(ACTIVE)
                )
            )
        ).scalar()
    )
    invoices = (
        await db.execute(select(PaymentInvoice).where(PaymentInvoice.status == "paid_waiting_credit"))
    ).scalars()
    pending = Decimal(0)
    for invoice in invoices:
        amount = paid_usdt(invoice)
        if amount is None:
            raise HTTPException(503, "provider_temporarily_unavailable")
        pending += amount
    required_float = get_settings().required_provider_float_usdt
    return {
        "balance": balance,
        "active": active,
        "pending": pending,
        "float": required_float,
        "available": balance - active - pending - required_float,
    }


async def require_provider_capital(db, cost_usdt, *, partner_id, provider="argolink"):
    await lock_capital(db)
    state = await provider_capital_state(db, partner_id, provider)
    if state["available"] < cost_usdt:
        raise HTTPException(503, "provider_temporarily_unavailable")
