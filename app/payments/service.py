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
from app.payments.lava_top import LavaTopClient, LavaTopError, LavaTopInvoice, get_lava_top_client
from app.payments.models import PaymentInvoice, PaymentRefund
from app.payments.schemas import normalize_buyer_email


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

    payment = await _ensure_invoice_fx_snapshot(db, payment=payment, client=client)

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


async def create_configured_invoice(
    db: AsyncSession,
    *,
    partner: Partner,
    requested_rub: int,
    idempotency_key: str,
    buyer_email: str | None = None,
    crypto_client: CryptoPayClient | None = None,
    lava_client: LavaTopClient | None = None,
) -> PaymentInvoice:
    """Create with the configured provider while preserving one local idempotency key."""
    from app.infrastructure.config import get_settings
    from app.payments.crypto_pay import get_crypto_pay_client

    settings = get_settings()
    if settings.payment_provider == "lava_top":
        effective_buyer_email = buyer_email or partner.payment_email
        if not effective_buyer_email:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="buyer_email_required")
        try:
            client = lava_client or get_lava_top_client()
        except LavaTopError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="payment_provider_not_configured",
            ) from exc
        return await create_or_resume_lava_invoice(
            db,
            partner=partner,
            requested_rub=requested_rub,
            idempotency_key=idempotency_key,
            buyer_email=effective_buyer_email,
            client=client,
        )
    return await create_or_resume_invoice(
        db,
        partner=partner,
        requested_rub=requested_rub,
        idempotency_key=idempotency_key,
        client=crypto_client or get_crypto_pay_client(),
    )


async def create_or_resume_lava_invoice(
    db: AsyncSession,
    *,
    partner: Partner,
    requested_rub: int,
    idempotency_key: str,
    buyer_email: str,
    client: LavaTopClient,
) -> PaymentInvoice:
    """Create one custom-price Lava.top SBP invoice.

    Lava.top does not expose a merchant idempotency key or invoice search by our
    identifier. Therefore an ambiguous create is never blindly retried; it is
    left for reconciliation by the returned contract UUID.
    """
    from app.billing.service import lock_partner_for_update

    partner = await lock_partner_for_update(db, partner.id)
    if partner.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="partner_not_active")

    normalized_email = normalize_buyer_email(buyer_email)
    payment = (
        await db.execute(
            select(PaymentInvoice).where(
                PaymentInvoice.partner_id == partner.id,
                PaymentInvoice.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if payment is not None:
        if payment.requested_rub != Decimal(requested_rub):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="idempotency_conflict")
        if payment.provider != "lava_top":
            return payment
        if payment.buyer_email and payment.buyer_email.lower() != normalized_email:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="idempotency_conflict")
        partner.payment_email = normalized_email
        if payment.status == "creating" and payment.provider_reference is None:
            claimed_until = payment.creation_claimed_until
            if claimed_until is not None and claimed_until.tzinfo is None:
                claimed_until = claimed_until.replace(tzinfo=UTC)
            if claimed_until is None or claimed_until <= utc_now():
                payment.status = "creation_unknown"
                payment.creation_claimed_until = None
                await db.flush()
        _mark_expired_if_needed(payment)
        return payment

    payment = PaymentInvoice(
        partner_id=partner.id,
        provider="lava_top",
        idempotency_key=idempotency_key,
        requested_rub=Decimal(requested_rub),
        accepted_assets="RUB:SBP",
        buyer_email=normalized_email,
        status="creating",
        creation_claimed_until=utc_now() + timedelta(seconds=60),
    )
    partner.payment_email = normalized_email
    db.add(payment)
    await db.flush()
    await db.commit()
    payment = await _ensure_persisted_fx_snapshot(db, payment=payment)

    try:
        provider_invoice = await client.create_sbp_invoice(
            amount_rub=requested_rub,
            buyer_email=normalized_email,
        )
    except LavaTopError as exc:
        payment = await _lock_payment(db, payment.id)
        payment.status = "creation_unknown"
        payment.creation_claimed_until = None
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="payment_provider_temporarily_unavailable",
        ) from exc

    payment = await _lock_payment(db, payment.id)
    if Decimal(provider_invoice.amount) != payment.requested_rub or provider_invoice.currency.upper() != "RUB":
        payment.provider_reference = provider_invoice.invoice_id
        payment.status = "creation_unknown"
        payment.creation_claimed_until = None
        payment.reconciliation_snapshot = {
            "provider_invoice_id": provider_invoice.invoice_id,
            "source": "create_response",
            "result": "amount_or_currency_mismatch",
        }
        await db.flush()
        return payment
    return await sync_lava_provider_invoice(
        db,
        provider_invoice=provider_invoice,
        payment_id=payment.id,
    )


async def refresh_configured_invoice(
    db: AsyncSession,
    *,
    payment_id: str,
    partner: Partner,
    crypto_client: CryptoPayClient | None = None,
    lava_client: LavaTopClient | None = None,
) -> PaymentInvoice:
    payment = await get_partner_payment(db, payment_id=payment_id, partner_id=partner.id)
    if payment.provider == "lava_top":
        if payment.provider_reference is None:
            return payment
        try:
            provider_invoice = await (lava_client or get_lava_top_client()).get_invoice(payment.provider_reference)
        except LavaTopError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="payment_provider_temporarily_unavailable",
            ) from exc
        if provider_invoice is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="payment_confirmation_pending",
            )
        return await sync_lava_provider_invoice(db, provider_invoice=provider_invoice, payment_id=payment.id)

    from app.payments.crypto_pay import get_crypto_pay_client

    client = crypto_client or get_crypto_pay_client()
    if payment.status in {"creating", "creation_unknown"}:
        payment = await create_or_resume_invoice(
            db,
            partner=partner,
            requested_rub=int(payment.requested_rub),
            idempotency_key=payment.idempotency_key,
            client=client,
        )
    if payment.provider_invoice_id and payment.status in {"active", "expired", "creating"}:
        try:
            provider_invoice = await client.get_invoice(payment.provider_invoice_id)
        except CryptoPayError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="payment_provider_temporarily_unavailable",
            ) from exc
        if provider_invoice and provider_invoice.status == "paid":
            payment = await apply_paid_provider_invoice(db, provider_invoice=provider_invoice)
        elif provider_invoice is not None:
            _apply_provider_invoice(payment, provider_invoice)
            await db.flush()
    return payment


async def cancel_configured_invoice(
    db: AsyncSession,
    *,
    payment_id: str,
    partner_id: str,
    crypto_client: CryptoPayClient | None = None,
) -> PaymentInvoice:
    payment = await get_partner_payment(db, payment_id=payment_id, partner_id=partner_id)
    if payment.provider == "lava_top":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="payment_not_cancellable")
    from app.payments.crypto_pay import get_crypto_pay_client

    return await cancel_active_invoice(
        db,
        payment_id=payment_id,
        partner_id=partner_id,
        client=crypto_client or get_crypto_pay_client(),
    )


async def sync_lava_provider_invoice(
    db: AsyncSession,
    *,
    provider_invoice: LavaTopInvoice,
    payment_id: str | None = None,
) -> PaymentInvoice:
    payment = (
        await db.execute(
            select(PaymentInvoice)
            .where(PaymentInvoice.provider_reference == provider_invoice.invoice_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if payment is None and payment_id is not None:
        payment = await _lock_payment(db, payment_id)
    if payment is None or payment.provider != "lava_top":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="payment_not_found")
    if payment.provider_reference not in {None, provider_invoice.invoice_id}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="payment_provider_id_conflict")
    if provider_invoice.currency.upper() != "RUB" or Decimal(provider_invoice.amount) != payment.requested_rub:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="payment_amount_or_currency_mismatch")
    if payment.buyer_email:
        if provider_invoice.status == "paid" and not provider_invoice.buyer_email:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="payment_buyer_mismatch")
        if (
            provider_invoice.buyer_email
            and payment.buyer_email.casefold() != provider_invoice.buyer_email.casefold()
        ):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="payment_buyer_mismatch")

    payment.provider_reference = provider_invoice.invoice_id
    if provider_invoice.payment_url:
        payment.invoice_url = provider_invoice.payment_url
    payment.creation_claimed_until = None
    if payment.credited_at is not None:
        return payment
    if provider_invoice.status == "paid":
        payment.status = "paid_waiting_credit"
        payment.paid_at = _parse_datetime(provider_invoice.paid_at) or utc_now()
        payment.paid_asset = "RUB"
        payment.paid_amount = payment.requested_rub
        payment.paid_fiat_rate = Decimal(1)
        payment.paid_usd_rate = await _rub_to_usdt_rate(db, payment)
        await db.flush()
        return await credit_paid_invoice(db, payment_id=payment.id)
    if provider_invoice.status in {"active", "failed", "cancelled"}:
        payment.status = provider_invoice.status
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
        .execution_options(populate_existing=True)
    )
    payment = payment_result.scalar_one_or_none()
    if payment is None and provider_invoice.payload:
        payment_result = await db.execute(
            select(PaymentInvoice)
            .where(PaymentInvoice.id == provider_invoice.payload)
            .with_for_update()
            .execution_options(populate_existing=True)
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
    payment.was_expired_when_paid = was_expired
    payment.creation_claimed_until = None
    await db.flush()
    return await credit_paid_invoice(db, payment_id=payment.id)


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
        description=f"Confirmed {payment.provider} payment credited",
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
    from app.billing.fx import current_fx, restore_snapshot
    from app.billing.fx import snapshot as fx_snapshot

    fx_data = restore_snapshot(payment.fx_snapshot)
    if fx_data is None:
        # Legacy invoices predate immutable invoice FX snapshots. Capture the
        # currently persisted/operator rate once; current_fx performs no network I/O.
        fx_data = await current_fx(db)
        payment.fx_snapshot = {**fx_snapshot(fx_data), "captured_for": "legacy_credit"}
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
        description=f"{payment.provider} payment funded real cost coverage",
        allow_negative=True,
    )
    payment.status = "credited"
    payment.credited_at = utc_now()
    from app.infrastructure.config import get_settings
    from app.telegram.service import notify

    await notify(
        db,
        partner.telegram_id,
        f"На баланс зачислено {payment.requested_rub:.2f} ₽. Платёж {payment.id}.",
        f"payment-credited:{payment.id}",
    )
    await notify(
        db,
        get_settings().admin_telegram_id,
        f"Оплачен счёт {payment.id}. Партнёру зачислено {payment.requested_rub:.2f} ₽.",
        f"payment-paid:{payment.id}",
    )
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
        description=f"Confirmed {payment.provider} refund",
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
        description=f"Confirmed {payment.provider} refund coverage reversal",
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


async def _ensure_invoice_fx_snapshot(
    db: AsyncSession,
    *,
    payment: PaymentInvoice,
    client: CryptoPayClient,
) -> PaymentInvoice:
    if payment.fx_snapshot is not None:
        return payment

    from app.billing.fx import refresh_fx_for_invoice
    from app.billing.fx import snapshot as fx_snapshot

    # External FX lookup happens before re-taking the payment row lock. The
    # creation claim makes this caller the only submitter; the second check
    # keeps the snapshot idempotent if a concurrent recovery raced us.
    fx_data = await refresh_fx_for_invoice(db, client=client)
    payment = await _lock_payment(db, payment.id)
    if payment.fx_snapshot is None:
        payment.fx_snapshot = fx_snapshot(fx_data)
    await db.commit()
    return payment


async def _ensure_persisted_fx_snapshot(
    db: AsyncSession,
    *,
    payment: PaymentInvoice,
) -> PaymentInvoice:
    if payment.fx_snapshot is not None:
        return payment
    from app.billing.fx import current_fx
    from app.billing.fx import snapshot as fx_snapshot

    fx_data = await current_fx(db)
    payment = await _lock_payment(db, payment.id)
    if payment.fx_snapshot is None:
        payment.fx_snapshot = fx_snapshot(fx_data)
    await db.commit()
    return payment


async def _rub_to_usdt_rate(db: AsyncSession, payment: PaymentInvoice) -> Decimal:
    from app.billing.fx import current_fx, restore_snapshot

    fx_data = restore_snapshot(payment.fx_snapshot)
    if fx_data is None:
        fx_data = await current_fx(db)
    rub_per_usdt = Decimal(fx_data["rate"])
    return (Decimal(1) / rub_per_usdt).quantize(Decimal("0.000000000000000001"))


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
