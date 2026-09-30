import json

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select, text

from app.accounts.models import Partner
from app.api.dependencies import DbSession, get_current_partner, require_admin
from app.infrastructure.config import get_settings
from app.payments.crypto_pay import CryptoPayError, get_crypto_pay_client
from app.payments.models import CryptoPayWebhookEvent, PaymentInvoice
from app.payments.schemas import (
    PaymentCreditRead,
    PaymentInvoiceCreate,
    PaymentInvoiceRead,
    PaymentReconcileCreate,
    PaymentRefundCreate,
    PaymentRefundRead,
)
from app.payments.security import verify_crypto_pay_signature
from app.payments.service import (
    apply_paid_provider_invoice,
    cancel_active_invoice,
    create_or_resume_invoice,
    credit_paid_invoice,
    get_partner_payment,
    record_confirmed_refund,
)

router = APIRouter()


@router.post("/invoices", response_model=PaymentInvoiceRead, status_code=status.HTTP_202_ACCEPTED)
async def create_invoice(
    payload: PaymentInvoiceCreate,
    db: DbSession,
    partner: Partner = Depends(get_current_partner),
) -> PaymentInvoice:
    return await create_or_resume_invoice(
        db,
        partner=partner,
        requested_rub=payload.requested_rub,
        idempotency_key=payload.idempotency_key,
        client=get_crypto_pay_client(),
    )


@router.get("/invoices/{payment_id}", response_model=PaymentInvoiceRead)
async def read_invoice(
    payment_id: str,
    db: DbSession,
    partner: Partner = Depends(get_current_partner),
) -> PaymentInvoice:
    return await get_partner_payment(db, payment_id=payment_id, partner_id=partner.id)


@router.post("/invoices/{payment_id}/cancel", response_model=PaymentInvoiceRead)
async def cancel_invoice(
    payment_id: str,
    db: DbSession,
    partner: Partner = Depends(get_current_partner),
) -> PaymentInvoice:
    return await cancel_active_invoice(
        db,
        payment_id=payment_id,
        partner_id=partner.id,
        client=get_crypto_pay_client(),
    )


@router.post(
    "/invoices/{payment_id}/credit",
    response_model=PaymentCreditRead,
    dependencies=[Depends(require_admin)],
)
async def credit_invoice(payment_id: str, db: DbSession) -> PaymentCreditRead:
    payment = await credit_paid_invoice(db, payment_id=payment_id)
    return PaymentCreditRead(
        payment_id=payment.id,
        status=payment.status,
        requested_rub=payment.requested_rub,
    )


@router.post(
    "/invoices/{payment_id}/refunds",
    response_model=PaymentRefundRead,
    dependencies=[Depends(require_admin)],
)
async def confirm_refund(
    payment_id: str,
    payload: PaymentRefundCreate,
    db: DbSession,
) -> PaymentRefundRead:
    refund, payment = await record_confirmed_refund(
        db,
        payment_id=payment_id,
        amount_rub=payload.amount_rub,
        idempotency_key=payload.idempotency_key,
        reason=payload.reason,
    )
    return PaymentRefundRead(
        payment_id=payment.id,
        refund_id=refund.id,
        amount_rub=refund.amount_rub,
        refunded_rub_total=payment.refunded_rub,
        payment_status=payment.status,
    )


@router.post("/crypto-pay/webhook", include_in_schema=False)
async def crypto_pay_webhook(
    request: Request,
    db: DbSession,
    crypto_pay_api_signature: str | None = Header(default=None),
) -> dict[str, bool]:
    settings = get_settings()
    if not settings.crypto_pay_api_token:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="crypto_pay_not_configured")

    raw_body = await request.body()
    if not verify_crypto_pay_signature(
        api_token=settings.crypto_pay_api_token,
        raw_body=raw_body,
        signature=crypto_pay_api_signature,
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_crypto_pay_signature")

    try:
        update = json.loads(raw_body)
        update_id = int(update["update_id"])
        update_type = str(update["update_type"])
        payload = update["payload"]
        provider_invoice_id = int(payload["invoice_id"]) if isinstance(payload, dict) else None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_crypto_pay_webhook") from exc

    # Serialize duplicate deliveries before recording the unique provider event.
    # The invoice lock separately protects different update IDs for one payment.
    if db.bind.dialect.name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"crypto-pay-update:{update_id}"},
        )
    existing_result = await db.execute(
        select(CryptoPayWebhookEvent).where(CryptoPayWebhookEvent.update_id == update_id)
    )
    if existing_result.scalar_one_or_none() is not None:
        return {"ok": True}

    if update_type == "invoice_paid" and provider_invoice_id is not None:
        try:
            provider_invoice = await get_crypto_pay_client().get_invoice(provider_invoice_id)
        except CryptoPayError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="payment_provider_temporarily_unavailable",
            ) from exc
        if provider_invoice is None or provider_invoice.status != "paid":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="payment_confirmation_pending",
            )
        await apply_paid_provider_invoice(db, provider_invoice=provider_invoice)

    db.add(
        CryptoPayWebhookEvent(
            update_id=update_id,
            update_type=update_type,
            provider_invoice_id=provider_invoice_id,
        )
    )
    await db.flush()
    return {"ok": True}


@router.post(
    "/invoices/{payment_id}/reconcile", response_model=PaymentInvoiceRead, dependencies=[Depends(require_admin)]
)
async def reconcile_invoice(payment_id: str, payload: PaymentReconcileCreate, db: DbSession):
    from decimal import Decimal

    from app.payments.service import _apply_provider_invoice, _lock_payment

    payment = await _lock_payment(db, payment_id)
    provider = await get_crypto_pay_client().get_invoice(payload.provider_invoice_id)
    if provider is None or provider.payload != payment.id or Decimal(provider.amount) != payment.requested_rub:
        raise HTTPException(409, "invoice_reconciliation_mismatch")
    if payment.provider_invoice_id not in {None, provider.invoice_id}:
        raise HTTPException(409, "payment_provider_id_conflict")
    if payment.credited_at is not None:
        return payment
    if (
        payment.reconciliation_snapshot
        and payment.reconciliation_snapshot["provider_invoice_id"] != provider.invoice_id
    ):
        raise HTTPException(409, "already_reconciled")
    if not payment.reconciliation_snapshot:
        payment.reconciliation_snapshot = {
            "provider_invoice_id": provider.invoice_id,
            "reason": payload.reason,
            "actor": "admin_api",
        }
    if provider.status == "paid":
        return await apply_paid_provider_invoice(db, provider_invoice=provider)
    _apply_provider_invoice(payment, provider)
    payment.creation_claimed_until = None
    await db.flush()
    return payment
