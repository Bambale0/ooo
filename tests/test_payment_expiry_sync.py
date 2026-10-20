"""Provider-confirmed expiry must update metadata, never erase confirmed money."""
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.accounts.models import Partner
from app.billing.models import LedgerEntry
from app.infrastructure.retry import utc_now
from app.payments.models import PaymentInvoice
from app.payments.reconciliation import reconcile_payments_once
from tests.test_crypto_payments import FakeCryptoPayClient


async def seed(db):
    partner = Partner(telegram_id="expiry-sync", company_name="test", project_name="test")
    db.add(partner)
    await db.flush()
    fake = FakeCryptoPayClient()
    invoice = await fake.create_rub_invoice(amount_rub="1500", payload="expiry-sync")
    fake.invoices[invoice.invoice_id] = replace(invoice, status="expired")
    payment = PaymentInvoice(id="expiry-sync", partner_id=partner.id,
        provider_invoice_id=invoice.invoice_id, status="active", requested_rub=Decimal("1500"),
        idempotency_key="expiry-sync")
    db.add(payment)
    await db.commit()
    return partner, payment, fake, invoice


async def run(db, fake):
    await reconcile_payments_once(session_factory=async_sessionmaker(db.bind, expire_on_commit=False), client=fake)


async def test_provider_expiry_sync_is_idempotent_and_has_no_money_effect(db_session):
    partner, payment, fake, _ = await seed(db_session)
    await run(db_session, fake)
    await db_session.refresh(payment)
    assert payment.status == "expired"
    snapshot = dict(payment.reconciliation_snapshot)
    assert snapshot["provider_status"] == "expired"
    await db_session.commit()
    await run(db_session, fake)
    await db_session.refresh(payment)
    await db_session.refresh(partner)
    assert payment.reconciliation_snapshot == snapshot
    assert partner.balance_rub == 0 and payment.credited_at is None
    assert await db_session.scalar(select(func.count()).select_from(LedgerEntry)) == 0


async def test_late_paid_invoice_after_expiry_still_credits_once(db_session):
    partner, payment, fake, invoice = await seed(db_session)
    await run(db_session, fake)
    await db_session.refresh(payment)
    assert payment.status == "expired"
    await db_session.commit()
    fake.invoices[invoice.invoice_id] = replace(invoice, status="paid", paid_asset="USDT", paid_amount="15")
    await run(db_session, fake)
    await run(db_session, fake)
    await db_session.refresh(payment)
    await db_session.refresh(partner)
    assert payment.status == "credited" and payment.was_expired_when_paid
    assert partner.balance_rub == Decimal("1500")
    assert await db_session.scalar(select(func.count()).select_from(LedgerEntry)) == 1


@pytest.mark.parametrize("field", ["invoice_id", "payload", "amount"])
async def test_unrelated_provider_expiry_cannot_change_local_invoice(db_session, field):
    _, payment, fake, invoice = await seed(db_session)
    value = invoice.invoice_id + 1 if field == "invoice_id" else "other" if field == "payload" else "9999"
    fake.invoices[invoice.invoice_id] = replace(invoice, status="expired", **{field: value})
    await run(db_session, fake)
    await db_session.refresh(payment)
    assert payment.status == "active" and payment.reconciliation_snapshot is None


async def test_paid_webhook_wins_race_against_stale_expired_read(db_session):
    partner, payment, fake, invoice = await seed(db_session)
    from app.payments.service import apply_paid_provider_invoice
    original = fake.get_invoice
    async def expired_after_payment(invoice_id):
        async with async_sessionmaker(db_session.bind, expire_on_commit=False)() as db:
            await apply_paid_provider_invoice(db, provider_invoice=replace(
                invoice, status="paid", paid_asset="USDT", paid_amount="15"))
            await db.commit()
        return await original(invoice_id)
    fake.get_invoice = expired_after_payment
    await run(db_session, fake)
    await db_session.refresh(payment)
    await db_session.refresh(partner)
    assert payment.status == "credited" and payment.credited_at is not None
    assert partner.balance_rub == Decimal("1500")


async def test_missing_invoice_does_not_infer_nonpayment(db_session):
    _, payment, fake, _ = await seed(db_session)
    fake.invoices.clear()
    await run(db_session, fake)
    await db_session.refresh(payment)
    assert payment.status == "active" and payment.credited_at is None


async def test_expired_reply_does_not_override_paid_waiting_credit(db_session):
    _, payment, fake, _ = await seed(db_session)
    payment.status = "paid_waiting_credit"
    payment.paid_at = utc_now()
    await db_session.commit()
    await run(db_session, fake)
    await db_session.refresh(payment)
    assert payment.status == "paid_waiting_credit"


@pytest.mark.parametrize("message", ["sk-private-sensitive-value", "secret in https://private.invalid/invoice?q=secret"])
async def test_provider_error_diagnostics_never_log_raw_secrets(db_session, caplog, message):
    _, _, fake, _ = await seed(db_session)
    from app.payments.crypto_pay import CryptoPayError
    async def broken(invoice_id):
        raise CryptoPayError(message)
    fake.get_invoice = broken
    await run(db_session, fake)
    record = next(r for r in caplog.records if r.message == "payment_reconciliation_failed")
    assert record.error_code == "payment_reconciliation_error"
    assert message not in str(record.__dict__)


async def test_transport_error_diagnostics_preserve_safe_reason(db_session, caplog):
    _, _, fake, _ = await seed(db_session)
    from app.payments.crypto_pay import CryptoPayError
    async def broken(invoice_id):
        raise CryptoPayError("ReadTimeout")
    fake.get_invoice = broken
    await run(db_session, fake)
    record = next(r for r in caplog.records if r.message == "payment_reconciliation_failed")
    assert record.error_type == "CryptoPayError" and record.error_code == "ReadTimeout"
