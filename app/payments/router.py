import hashlib
import json
import secrets
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select, text

from app.accounts.models import Partner
from app.api.dependencies import DbSession, get_current_partner, require_admin
from app.infrastructure.config import get_settings
from app.payments.crypto_pay import CryptoPayError, get_crypto_pay_client
from app.payments.lava_top import LavaTopError, get_lava_top_client
from app.payments.models import CryptoPayWebhookEvent, LavaTopWebhookEvent, PaymentInvoice
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
    cancel_configured_invoice,
    create_configured_invoice,
    credit_paid_invoice,
    get_partner_payment,
    record_confirmed_refund,
    sync_lava_provider_invoice,
)
from app.telegram.models import DevelopmentInvoice

router = APIRouter()


@router.post("/invoices", response_model=PaymentInvoiceRead, status_code=status.HTTP_202_ACCEPTED)
async def create_invoice(
    payload: PaymentInvoiceCreate,
    db: DbSession,
    partner: Partner = Depends(get_current_partner),
) -> PaymentInvoice:
    settings = get_settings()
    lava_client = None
    if settings.payment_provider == "lava_top":
        if not payload.buyer_email and not partner.payment_email:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="buyer_email_required")
        try:
            lava_client = get_lava_top_client()
        except LavaTopError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="payment_provider_not_configured",
            ) from exc
    return await create_configured_invoice(
        db,
        partner=partner,
        requested_rub=payload.requested_rub,
        idempotency_key=payload.idempotency_key,
        buyer_email=payload.buyer_email,
        crypto_client=get_crypto_pay_client() if settings.payment_provider == "crypto_pay" else None,
        lava_client=lava_client,
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
    payment = await get_partner_payment(db, payment_id=payment_id, partner_id=partner.id)
    return await cancel_configured_invoice(
        db,
        payment_id=payment_id,
        partner_id=partner.id,
        crypto_client=get_crypto_pay_client() if payment.provider == "crypto_pay" else None,
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


@router.post("/lava-top/webhook", include_in_schema=False)
async def lava_top_webhook(
    request: Request,
    db: DbSession,
    x_api_key: str | None = Header(default=None),
) -> dict[str, bool]:
    settings = get_settings()
    if not settings.lava_top_webhook_secret:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="lava_top_webhook_not_configured")
    expected = settings.lava_top_webhook_secret.get_secret_value()
    if x_api_key is None or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_lava_top_webhook_secret")

    raw_body = await request.body()
    if len(raw_body) > 1024 * 1024:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="webhook_too_large")
    try:
        update = json.loads(raw_body)
        if not isinstance(update, dict):
            raise TypeError
        event_type_value = update.get("eventType", update.get("event_type"))
        if event_type_value is None:
            raise KeyError("eventType")
        event_type = str(event_type_value)
        provider_reference_value = update.get("contractId")
        if provider_reference_value is None and isinstance(update.get("data"), dict):
            provider_reference_value = update["data"].get("contractId")
        provider_reference = str(provider_reference_value) if provider_reference_value else None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_lava_top_webhook") from exc
    if not event_type or len(event_type) > 80 or (provider_reference and len(provider_reference) > 80):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_lava_top_webhook")
    if event_type.startswith("payment.") and not provider_reference:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_lava_top_webhook")

    event_key = hashlib.sha256(raw_body).hexdigest()
    if db.bind.dialect.name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"lava-top-event:{event_key}"},
        )
    existing = await db.scalar(select(LavaTopWebhookEvent).where(LavaTopWebhookEvent.event_key == event_key))
    if existing is not None:
        return {"ok": True}

    local_payment = None
    development_payment = None
    if provider_reference:
        local_payment = await db.scalar(
            select(PaymentInvoice).where(
                PaymentInvoice.provider == "lava_top",
                PaymentInvoice.provider_reference == provider_reference,
            )
        )
        if local_payment is None:
            development_payment = await db.scalar(
                select(DevelopmentInvoice).where(
                    DevelopmentInvoice.provider == "lava_top",
                    DevelopmentInvoice.provider_reference == provider_reference,
                )
            )
    if (local_payment is not None or development_payment is not None) and event_type.startswith("payment."):
        try:
            provider_invoice = await get_lava_top_client().get_invoice(provider_reference)
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
        if local_payment is not None:
            await sync_lava_provider_invoice(
                db,
                provider_invoice=provider_invoice,
                payment_id=local_payment.id,
            )
        else:
            from app.telegram.development_billing import sync_development_invoice

            await sync_development_invoice(
                db,
                provider_invoice=provider_invoice,
                invoice_id=development_payment.id,
            )

    db.add(
        LavaTopWebhookEvent(
            event_key=event_key,
            event_type=event_type,
            provider_reference=provider_reference,
        )
    )
    await db.flush()
    return {"ok": True}


@router.post(
    "/invoices/{payment_id}/reconcile", response_model=PaymentInvoiceRead, dependencies=[Depends(require_admin)]
)
async def reconcile_invoice(payment_id: str, payload: PaymentReconcileCreate, db: DbSession):
    from app.payments.service import _apply_provider_invoice, _lock_payment

    payment = await _lock_payment(db, payment_id)
    if payment.provider == "lava_top":
        provider_reference = str(payload.provider_invoice_id)
        try:
            provider = await get_lava_top_client().get_invoice(provider_reference)
        except LavaTopError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="payment_provider_temporarily_unavailable",
            ) from exc
        if provider is None or provider.currency.upper() != "RUB" or Decimal(provider.amount) != payment.requested_rub:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="invoice_reconciliation_mismatch")
        if payment.provider_reference not in {None, provider.invoice_id}:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="payment_provider_id_conflict")
        if (
            payment.reconciliation_snapshot
            and payment.reconciliation_snapshot.get("provider_invoice_id") != provider.invoice_id
        ):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="already_reconciled")
        if not payment.reconciliation_snapshot:
            payment.reconciliation_snapshot = {
                "provider_invoice_id": provider.invoice_id,
                "reason": payload.reason,
                "actor": "admin_api",
            }
        return await sync_lava_provider_invoice(db, provider_invoice=provider, payment_id=payment.id)

    try:
        crypto_provider_invoice_id = int(payload.provider_invoice_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid_provider_invoice_id",
        ) from exc
    provider = await get_crypto_pay_client().get_invoice(crypto_provider_invoice_id)
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
