"""Recover confirmed payments even when a webhook or creation response is lost."""

import asyncio
import logging
from contextlib import suppress
from decimal import Decimal

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.infrastructure.config import get_settings
from app.infrastructure.database import SessionLocal
from app.infrastructure.retry import utc_now
from app.payments.crypto_pay import CryptoPayClient, CryptoPayInvoice, get_crypto_pay_client
from app.payments.models import PaymentInvoice
from app.payments.service import apply_paid_provider_invoice

logger = logging.getLogger(__name__)


def _safe_error_details(exc: Exception) -> dict:
    # Never log raw provider messages, invoice URLs, payloads or credentials.
    allowed = {
        "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "ConnectError",
        "ReadError", "RemoteProtocolError", "HTTPStatusError", "invalid_invoice_list",
        "invalid_crypto_pay_invoice", "invalid_provider_response", "crypto_pay_request_failed",
        "payment_provider_id_conflict", "payment_amount_or_payload_mismatch",
    }
    code = str(exc)
    cause = exc.__cause__
    return {
        "error_type": type(exc).__name__,
        "error_code": code if code in allowed else "payment_reconciliation_error",
        "upstream_status": cause.response.status_code if isinstance(cause, httpx.HTTPStatusError) else None,
    }


async def _record_provider_expiry(db: AsyncSession, payment_id: str, invoice: CryptoPayInvoice) -> bool:
    """Record only verified expiry; a concurrent paid webhook always wins."""
    payment = await db.scalar(select(PaymentInvoice).where(
        PaymentInvoice.id == payment_id
    ).with_for_update().execution_options(populate_existing=True))
    if payment is None or payment.provider != "crypto_pay":
        return False
    if payment.provider_invoice_id not in {None, invoice.invoice_id}:
        raise ValueError("payment_provider_id_conflict")
    if invoice.payload not in {None, payment.id} or Decimal(invoice.amount) != payment.requested_rub:
        raise ValueError("payment_amount_or_payload_mismatch")
    if (invoice.status != "expired" or payment.credited_at is not None or payment.paid_at is not None
        or payment.status not in {"active", "creating", "creation_unknown"}):
        return False
    payment.provider_invoice_id = invoice.invoice_id
    payment.status = "expired"
    payment.creation_claimed_until = None
    payment.reconciliation_snapshot = {
        **(payment.reconciliation_snapshot or {}),
        "provider_status": "expired", "provider_status_checked_at": utc_now().isoformat(),
        "source": "provider_invoice_read",
    }
    await db.flush()
    return True


async def reconcile_payments_once(
    *,
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
    client: CryptoPayClient,
    limit: int = 20,
    after_id: str | None = None,
) -> str | None:
    query = select(PaymentInvoice.id, PaymentInvoice.provider_invoice_id).where(
        PaymentInvoice.provider == "crypto_pay",
        PaymentInvoice.credited_at.is_(None),
        PaymentInvoice.status.in_(("active", "expired", "paid_waiting_credit", "creation_unknown", "creating")),
        or_(PaymentInvoice.creation_claimed_until.is_(None), PaymentInvoice.creation_claimed_until <= utc_now()),
    )
    if after_id is not None:
        query = query.where(PaymentInvoice.id > after_id)
    async with session_factory() as db:
        candidates = (await db.execute(query.order_by(PaymentInvoice.id).limit(limit))).all()

    for payment_id, provider_id in candidates:
        try:
            # Provider reads are outside the DB transaction. Never create or pay an invoice here.
            invoice = (
                await client.get_invoice(provider_id)
                if provider_id is not None
                else await client.find_invoice_by_payload(payment_id)
            )
            if invoice is None:
                continue
            if (provider_id is not None and invoice.invoice_id != provider_id) or (
                provider_id is None and invoice.payload != payment_id
            ):
                raise ValueError("payment_provider_id_conflict")
            if invoice.status == "expired":
                async with session_factory() as db:
                    changed = await _record_provider_expiry(db, payment_id, invoice)
                    await db.commit()
                if changed:
                    logger.info("payment_expiry_reconciled", extra={"payment_id": payment_id})
            elif invoice.status == "paid":
                async with session_factory() as db:
                    await apply_paid_provider_invoice(db, provider_invoice=invoice)
                    await db.commit()
        except Exception as exc:
            # Session closure rolls back the entire credit. A bad invoice cannot starve the batch.
            logger.warning(
                "payment_reconciliation_failed",
                extra={"payment_id": payment_id, **_safe_error_details(exc)},
            )
    return candidates[-1][0] if len(candidates) == limit else None


async def payment_reconciliation_loop(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    if not settings.crypto_pay_api_token:
        return
    cursor = None
    logger.info("payment_reconciliation_started")
    while not stop_event.is_set():
        try:
            cursor = await reconcile_payments_once(
                client=get_crypto_pay_client(),
                limit=settings.payment_reconciliation_batch_size,
                after_id=cursor,
            )
        except Exception as exc:
            logger.warning("payment_reconciliation_cycle_failed", extra=_safe_error_details(exc))
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=settings.payment_reconciliation_interval_seconds)
