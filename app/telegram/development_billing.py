"""Admin-issued development invoices, isolated from API balance billing."""

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.models import Partner
from app.infrastructure.config import get_settings
from app.infrastructure.retry import utc_now
from app.payments.lava_top import LavaTopBuyerEmailRejected, LavaTopClient, LavaTopError
from app.payments.schemas import normalize_buyer_email
from app.referrals.service import DEVELOPMENT_ORDER_RATE, accrue_commission
from app.telegram.models import DevelopmentInvoice, DevelopmentRequest
from app.telegram.service import notify

ADVANCE_RATE = Decimal("0.30")
WHOLE_RUBLE = Decimal("1")


def installment_amount(total_price_rub: Decimal, installment: str) -> Decimal:
    total = Decimal(total_price_rub)
    advance = (total * ADVANCE_RATE).quantize(WHOLE_RUBLE, rounding=ROUND_HALF_UP)
    if installment == "advance":
        return advance
    if installment == "final":
        return total - advance
    raise ValueError("invalid development installment")


async def set_quote(
    db: AsyncSession,
    *,
    request_id: str,
    total_price_rub: Decimal,
    buyer_email: str,
    actor: str,
) -> DevelopmentRequest:
    row = await db.scalar(
        select(DevelopmentRequest).where(DevelopmentRequest.id == request_id).with_for_update()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="development_request_not_found")
    has_invoice = await db.scalar(
        select(func.count()).select_from(DevelopmentInvoice).where(
            DevelopmentInvoice.development_request_id == row.id
        )
    )
    if has_invoice:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="development_quote_locked")
    amount = Decimal(total_price_rub)
    if amount <= 0 or amount != amount.quantize(WHOLE_RUBLE):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid_development_price")
    row.quoted_price_rub = amount
    row.buyer_email = normalize_buyer_email(buyer_email)
    row.quoted_by = actor
    row.quoted_at = utc_now()
    await db.flush()
    return row


async def create_development_invoice(
    db: AsyncSession,
    *,
    request_id: str,
    installment: str,
    idempotency_key: str,
    client: LavaTopClient,
) -> DevelopmentInvoice:
    """Create one 30/70 Lava.top invoice using the dedicated development offer."""
    if installment not in {"advance", "final"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid_installment")
    settings = get_settings()
    if not settings.lava_top_development_offer_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="development_payments_not_configured",
        )
    row = await db.scalar(
        select(DevelopmentRequest).where(DevelopmentRequest.id == request_id).with_for_update()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="development_request_not_found")
    if row.quoted_price_rub is None or not row.buyer_email:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="development_quote_required")

    existing = await db.scalar(
        select(DevelopmentInvoice).where(
            DevelopmentInvoice.development_request_id == row.id,
            DevelopmentInvoice.installment == installment,
        )
    )
    if existing is not None:
        if (
            existing.total_price_snapshot_rub != row.quoted_price_rub
            or existing.buyer_email.casefold() != row.buyer_email.casefold()
        ):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="development_invoice_conflict")
        if existing.provider_reference and existing.settled_at is None:
            try:
                provider_invoice = await client.get_invoice(existing.provider_reference)
            except LavaTopError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="payment_provider_temporarily_unavailable",
                ) from exc
            if provider_invoice is not None:
                return await sync_development_invoice(
                    db,
                    provider_invoice=provider_invoice,
                    invoice_id=existing.id,
                )
        return existing

    if installment == "final":
        advance = await db.scalar(
            select(DevelopmentInvoice).where(
                DevelopmentInvoice.development_request_id == row.id,
                DevelopmentInvoice.installment == "advance",
            )
        )
        if advance is None or advance.settled_at is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="development_advance_not_paid")

    amount = installment_amount(row.quoted_price_rub, installment)
    if amount <= 0:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid_development_price")
    invoice = DevelopmentInvoice(
        development_request_id=row.id,
        installment=installment,
        idempotency_key=idempotency_key,
        status="creating",
        total_price_snapshot_rub=row.quoted_price_rub,
        requested_rub=amount,
        buyer_email=row.buyer_email,
        creation_claimed_until=utc_now() + timedelta(seconds=60),
    )
    db.add(invoice)
    await db.flush()
    # Persist the local claim before the external side effect. An ambiguous
    # provider response is never blindly submitted a second time.
    await db.commit()

    try:
        provider_invoice = await client.create_sbp_invoice(
            amount_rub=int(amount),
            buyer_email=row.buyer_email,
            offer_id=settings.lava_top_development_offer_id,
        )
    except LavaTopBuyerEmailRejected as exc:
        invoice = await _lock_invoice(db, invoice.id)
        invoice.status = "failed"
        invoice.creation_claimed_until = None
        invoice.reconciliation_snapshot = {"source": "create_response", "result": "buyer_email_rejected"}
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="buyer_email_rejected_by_provider",
        ) from exc
    except LavaTopError as exc:
        invoice = await _lock_invoice(db, invoice.id)
        invoice.status = "creation_unknown"
        invoice.creation_claimed_until = None
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="payment_provider_temporarily_unavailable",
        ) from exc

    # Persist the provider identity before validation/settlement. If the process
    # stops after Lava.top created the invoice, reconciliation can still recover
    # it without submitting a duplicate checkout.
    invoice = await _lock_invoice(db, invoice.id)
    if invoice.provider_reference not in {None, provider_invoice.invoice_id}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="payment_provider_id_conflict")
    invoice.provider_reference = provider_invoice.invoice_id
    invoice.invoice_url = provider_invoice.payment_url or invoice.invoice_url
    invoice.status = "creation_unknown"
    invoice.creation_claimed_until = None
    await db.commit()

    return await sync_development_invoice(
        db,
        provider_invoice=provider_invoice,
        invoice_id=invoice.id,
    )


async def sync_development_invoice(
    db: AsyncSession,
    *,
    provider_invoice,
    invoice_id: str | None = None,
) -> DevelopmentInvoice:
    invoice = await db.scalar(
        select(DevelopmentInvoice)
        .where(DevelopmentInvoice.provider_reference == provider_invoice.invoice_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if invoice is None and invoice_id is not None:
        invoice = await _lock_invoice(db, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="development_invoice_not_found")
    if invoice.provider_reference not in {None, provider_invoice.invoice_id}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="payment_provider_id_conflict")
    if provider_invoice.currency.upper() != "RUB" or Decimal(provider_invoice.amount) != invoice.requested_rub:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="payment_amount_or_currency_mismatch")
    if provider_invoice.status == "paid" and not provider_invoice.buyer_email:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="payment_buyer_mismatch")
    if provider_invoice.buyer_email and provider_invoice.buyer_email.casefold() != invoice.buyer_email.casefold():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="payment_buyer_mismatch")

    invoice.provider_reference = provider_invoice.invoice_id
    invoice.creation_claimed_until = None
    if provider_invoice.payment_url:
        invoice.invoice_url = provider_invoice.payment_url
    row = await db.get(DevelopmentRequest, invoice.development_request_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="development_request_not_found")

    if provider_invoice.status == "paid":
        if invoice.settled_at is not None:
            return invoice
        invoice.status = "paid"
        invoice.paid_at = _provider_paid_at(provider_invoice.paid_at)
        invoice.settled_at = utc_now()
        commission = await accrue_commission(
            db,
            referred_telegram_id=row.telegram_id,
            kind="development_order",
            source_id=invoice.id,
            event_key=f"development-payment:{invoice.id}",
            base_amount_rub=invoice.requested_rub,
            rate=DEVELOPMENT_ORDER_RATE,
        )
        if commission is not None:
            referrer = await db.get(Partner, commission.referrer_partner_id)
            if referrer is not None:
                await notify(
                    db,
                    referrer.telegram_id,
                    f"Партнёрское начисление: {commission.amount_rub:.2f} ₽ "
                    f"(20% от оплаченной части заказа на разработку).",
                    f"referral-commission:{commission.event_key}",
                )
        label = "аванс 30%" if invoice.installment == "advance" else "остаток 70%"
        await notify(
            db,
            row.telegram_id,
            f"Оплата по заявке {row.id[:8]} подтверждена: {label}, {invoice.requested_rub:.2f} ₽.",
            f"development-invoice-paid:{invoice.id}",
        )
        await notify(
            db,
            get_settings().admin_telegram_id,
            f"Оплачен {label} по заявке {row.id}: {invoice.requested_rub:.2f} ₽.",
            f"development-invoice-paid-admin:{invoice.id}",
        )
    elif provider_invoice.status in {"active", "failed", "cancelled"}:
        invoice.status = provider_invoice.status

    if invoice.invoice_url:
        label = "аванс 30%" if invoice.installment == "advance" else "остаток 70%"
        await notify(
            db,
            row.telegram_id,
            f"Выставлен счёт по заявке {row.id[:8]}: {label}, {invoice.requested_rub:.2f} ₽.\n"
            f"Оплатить: {invoice.invoice_url}\n"
            f"Чек после оплаты будет отправлен на {invoice.buyer_email}.",
            f"development-invoice-issued:{invoice.id}",
        )
    await db.flush()
    await db.refresh(invoice)
    return invoice


async def _lock_invoice(db: AsyncSession, invoice_id: str) -> DevelopmentInvoice:
    invoice = await db.scalar(
        select(DevelopmentInvoice)
        .where(DevelopmentInvoice.id == invoice_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="development_invoice_not_found")
    return invoice


def _provider_paid_at(value: str | None):
    if not value:
        return utc_now()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return utc_now()
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
