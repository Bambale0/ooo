from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.models import Partner
from app.billing.service import apply_cost_coverage_change, apply_partner_balance_change
from app.infrastructure.retry import utc_now
from app.payments.crypto_pay import CryptoPayClient, CryptoPayError, CryptoPayInvoice
from app.payments.models import PaymentInvoice, PaymentRefund


async def create_or_resume_invoice(
    db: AsyncSession,
    *,
    partner: Partner,
    requested_rub: int,
    idempotency_key: str,
    client: CryptoPayClient,
) -> PaymentInvoice:
    from app.billing.service import lock_partner_for_update

    partner = await lock_partner_for_update(db, partner.id)
    if partner.status != "active":
        raise HTTPException(403, "partner_not_active")
    existing_result = await db.execute(
        select(PaymentInvoice).where(
            PaymentInvoice.partner_id == partner.id,
            PaymentInvoice.idempotency_key == idempotency_key,
        )
    )
    payment = existing_result.scalar_one_or_none()
    may_submit = payment is None
    if payment is not None and payment.requested_rub != Decimal(requested_rub):
        raise HTTPException(409, "idempotency_conflict")
    if payment is None:
        payment = PaymentInvoice(
            partner_id=partner.id,
            idempotency_key=idempotency_key,
            requested_rub=Decimal(requested_rub),
            status="creating",
            creation_claimed_until=utc_now() + timedelta(seconds=60),
        )
        db.add(payment)
        await db.flush()
        await db.commit()
    elif payment.provider_invoice_id is not None or payment.status not in {"creating", "creation_unknown"}:
        _mark_expired_if_needed(payment)
        return payment
    elif (
        payment.creation_claimed_until is not None
        and (
            payment.creation_claimed_until.replace(tzinfo=UTC)
            if payment.creation_claimed_until.tzinfo is None
            else payment.creation_claimed_until
        )
        > utc_now()
    ):
        return payment
    else:
        payment.creation_claimed_until = utc_now() + timedelta(seconds=60)
        await db.commit()

    try:
        provider_invoice = await client.find_invoice_by_payload(payment.id)
        if provider_invoice is None and may_submit:
            provider_invoice = await client.create_rub_invoice(
                amount_rub=str(int(payment.requested_rub)),
                payload=payment.id,
            )
    except CryptoPayError as exc:
        payment = await _lock_payment(db, payment.id)
        if payment.status == "creating":
            payment.status, payment.creation_claimed_until = "creation_unknown", None
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="payment_provider_temporarily_unavailable",
        ) from exc

    payment = await _lock_payment(db, payment.id)
    if payment.credited_at is not None or payment.status == "paid_waiting_credit":
        return payment
    if provider_invoice is None:
        payment.status, payment.creation_claimed_until = "creation_unknown", None
        await db.flush()
        return payment
    if provider_invoice.status == "paid":
        return await apply_paid_provider_invoice(db, provider_invoice=provider_invoice)
    _apply_provider_invoice(payment, provider_invoice)
    payment.creation_claimed_until = None
    await db.flush()
    await db.refresh(payment)
    return payment


async def cancel_active_invoice(
    db: AsyncSession,
    *,
    payment_id: str,
    partner_id: str,
    client: CryptoPayClient,
) -> PaymentInvoice:
    payment = await _lock_payment(db, payment_id)
    if payment.partner_id != partner_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="payment_not_found")
    _mark_expired_if_needed(payment)
    if payment.status in {"cancelled", "expired"}:
        return payment
    if payment.status != "active" or payment.provider_invoice_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="payment_not_cancellable")

    try:
        await client.delete_invoice(payment.provider_invoice_id)
    except CryptoPayError as exc:
        try:
            provider = await client.get_invoice(payment.provider_invoice_id)
        except CryptoPayError:
            provider = False
        if provider is None:
            payment.status = "cancelled"
            await db.flush()
            return payment
        if provider and provider.status == "paid":
            return await apply_paid_provider_invoice(db, provider_invoice=provider)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="payment_provider_temporarily_unavailable",
        ) from exc
    payment.status = "cancelled"
    await db.flush()
    await db.refresh(payment)
    return payment


async def apply_paid_provider_invoice(
    db: AsyncSession,
    *,
    provider_invoice: CryptoPayInvoice,
) -> PaymentInvoice:
    payment_result = await db.execute(
        select(PaymentInvoice)
        .where(PaymentInvoice.provider_invoice_id == provider_invoice.invoice_id)
        .with_for_update()
    )
    payment = payment_result.scalar_one_or_none()
    if payment is None and provider_invoice.payload:
        payment_result = await db.execute(
            select(PaymentInvoice).where(PaymentInvoice.id == provider_invoice.payload).with_for_update()
        )
        payment = payment_result.scalar_one_or_none()
    if payment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="payment_not_found")
    if Decimal(provider_invoice.amount) != payment.requested_rub or provider_invoice.payload not in {None, payment.id}:
        raise HTTPException(409, "payment_amount_or_payload_mismatch")
    if provider_invoice.status != "paid":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="payment_confirmation_pending",
        )

    if payment.provider_invoice_id is None:
        payment.provider_invoice_id = provider_invoice.invoice_id
    elif payment.provider_invoice_id != provider_invoice.invoice_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="payment_provider_id_conflict")

    if payment.credited_at is not None:
        return payment

    was_expired = payment.status == "expired"
    expires_at_2 = payment.expires_at
    if expires_at_2 is not None and expires_at_2.tzinfo is None:
        expires_at_2 = expires_at_2.replace(tzinfo=UTC)
    if expires_at_2 is not None and expires_at_2 <= utc_now():
        was_expired = True

    _apply_provider_invoice(payment, provider_invoice)
    payment.status = "paid_waiting_credit"
    from app.infrastructure.config import get_settings
    from app.telegram.service import notify

    await notify(
        db,
        get_settings().admin_telegram_id,
        f"Оплачен счёт {payment.id}. Проверьте раздел «Платежи».",
        f"payment-paid:{payment.id}",
    )
    payment.was_expired_when_paid = was_expired
    payment.creation_claimed_until = None
    await db.flush()
    await db.refresh(payment)
    return payment


async def credit_paid_invoice(db: AsyncSession, *, payment_id: str) -> PaymentInvoice:
    payment = await _lock_payment(db, payment_id)
    if payment.credited_at is not None:
        return payment
    if payment.status != "paid_waiting_credit":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="payment_not_ready_for_credit")

    partner = await db.get(Partner, payment.partner_id)
    if partner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="partner_not_found")

    amount = Decimal(payment.requested_rub)
    await apply_partner_balance_change(
        db=db,
        partner=partner,
        amount_rub=amount,
        operation_type="payment_credit",
        idempotency_key=f"payment-credit-retail:{payment.id}",
        description="Crypto Pay payment manually credited",
        allow_negative=True,
    )
    from app.catalog.models import Model, PartnerPrice

    prices = (
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
    from app.billing.fx import current_fx
    from app.billing.fx import snapshot as fx_snapshot

    fx_data = await current_fx(db)
    fx = fx_data["rate"]
    ratio = max((p.provider_cost_usdt * fx / p.price_rub for p in prices), default=Decimal(1))
    coverage = (amount * ratio).quantize(Decimal(".01"), rounding="ROUND_HALF_UP")
    payment.coverage_snapshot = {
        "fx": fx_snapshot(fx_data),
        "ratio": str(ratio),
        "rub_per_usdt": str(fx),
        "coverage_rub": str(coverage),
    }
    await apply_cost_coverage_change(
        db=db,
        partner=partner,
        amount_rub=coverage,
        operation_type="payment_coverage_credit",
        idempotency_key=f"payment-credit-coverage:{payment.id}",
        description="Crypto Pay payment funded real cost coverage",
        allow_negative=True,
    )
    payment.status = "credited"
    payment.credited_at = utc_now()
    await db.flush()
    await db.refresh(payment)
    return payment


async def record_confirmed_refund(
    db: AsyncSession,
    *,
    payment_id: str,
    amount_rub: Decimal,
    idempotency_key: str,
    reason: str,
) -> tuple[PaymentRefund, PaymentInvoice]:
    payment = await _lock_payment(db, payment_id)
    existing_result = await db.execute(select(PaymentRefund).where(PaymentRefund.idempotency_key == idempotency_key))
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        if existing.payment_invoice_id != payment.id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="refund_idempotency_conflict")
        return existing, payment

    if payment.credited_at is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="payment_not_credited")
    remaining = Decimal(payment.requested_rub) - Decimal(payment.refunded_rub)
    amount = Decimal(amount_rub)
    if amount > remaining:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="refund_exceeds_remaining_payment")

    partner = await db.get(Partner, payment.partner_id)
    if partner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="partner_not_found")

    refund = PaymentRefund(
        payment_invoice_id=payment.id,
        amount_rub=amount,
        idempotency_key=idempotency_key,
        reason=reason,
    )
    db.add(refund)
    await db.flush()
    await apply_partner_balance_change(
        db=db,
        partner=partner,
        amount_rub=-amount,
        operation_type="payment_refund_adjustment",
        idempotency_key=f"payment-refund-retail:{refund.id}",
        description="Confirmed Crypto Pay refund",
        allow_negative=True,
    )
    ratio = Decimal((payment.coverage_snapshot or {}).get("ratio", "1"))
    # Difference of rounded cumulative reversals makes a sequence of partial
    # refunds add up exactly to the original immutable coverage snapshot.
    old_reversed = (Decimal(payment.refunded_rub) * ratio).quantize(Decimal(".01"), rounding="ROUND_HALF_UP")
    new_reversed = ((Decimal(payment.refunded_rub) + amount) * ratio).quantize(Decimal(".01"), rounding="ROUND_HALF_UP")
    coverage_reversal = new_reversed - old_reversed
    await apply_cost_coverage_change(
        db=db,
        partner=partner,
        amount_rub=-coverage_reversal,
        operation_type="payment_refund_coverage_adjustment",
        idempotency_key=f"payment-refund-coverage:{refund.id}",
        description="Confirmed Crypto Pay refund coverage reversal",
        allow_negative=True,
    )
    payment.refunded_rub = Decimal(payment.refunded_rub) + amount
    payment.status = (
        "refunded" if Decimal(payment.refunded_rub) == Decimal(payment.requested_rub) else "partially_refunded"
    )
    await db.flush()
    await db.refresh(refund)
    await db.refresh(payment)
    return refund, payment


async def get_partner_payment(
    db: AsyncSession,
    *,
    payment_id: str,
    partner_id: str,
) -> PaymentInvoice:
    payment = await db.get(PaymentInvoice, payment_id)
    if payment is None or payment.partner_id != partner_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="payment_not_found")
    _mark_expired_if_needed(payment)
    return payment


async def _lock_payment(db: AsyncSession, payment_id: str) -> PaymentInvoice:
    result = await db.execute(
        select(PaymentInvoice)
        .where(PaymentInvoice.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    payment = result.scalar_one_or_none()
    if payment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="payment_not_found")
    return payment


def _apply_provider_invoice(payment: PaymentInvoice, invoice: CryptoPayInvoice) -> None:
    payment.provider_invoice_id = invoice.invoice_id
    payment.invoice_url = invoice.bot_invoice_url
    payment.expires_at = _parse_datetime(invoice.expiration_date)
    if invoice.status == "active":
        payment.status = "active"
    elif invoice.status == "expired":
        payment.status = "expired"
    payment.paid_at = _parse_datetime(invoice.paid_at)
    payment.paid_asset = invoice.paid_asset
    payment.paid_amount = Decimal(invoice.paid_amount) if invoice.paid_amount else None
    payment.paid_fiat_rate = Decimal(invoice.paid_fiat_rate) if invoice.paid_fiat_rate else None
    payment.paid_usd_rate = Decimal(invoice.paid_usd_rate) if invoice.paid_usd_rate else None


def _mark_expired_if_needed(payment: PaymentInvoice) -> None:
    expires_at = payment.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if payment.status == "active" and expires_at is not None and expires_at <= utc_now():
        payment.status = "expired"


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
