from dataclasses import replace
from decimal import Decimal

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.accounts.models import Partner
from app.payments.models import PaymentInvoice
from tests.test_crypto_payments import FakeCryptoPayClient


async def test_recovery_rotates_past_unpaid_and_bad_invoices_and_credits_late_payment(db_session):
    from app.payments.reconciliation import reconcile_payments_once

    partner = Partner(telegram_id="recovery", company_name="Recovery", project_name="test")
    db_session.add(partner)
    await db_session.flush()
    fake = FakeCryptoPayClient()
    for local_id, state in [("a", "active"), ("b", "active"), ("c", "expired")]:
        invoice = await fake.create_rub_invoice(amount_rub="1500", payload=local_id)
        if local_id != "a":
            fake.invoices[invoice.invoice_id] = replace(
                invoice, status="paid", amount="9000" if local_id == "b" else "1500",
                paid_asset="TON", paid_amount="3",
            )
        db_session.add(PaymentInvoice(
            id=local_id, partner_id=partner.id, provider_invoice_id=invoice.invoice_id,
            idempotency_key=local_id, status=state, requested_rub=Decimal("1500"),
        ))
    await db_session.commit()
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    cursor = await reconcile_payments_once(session_factory=factory, client=fake, limit=2)
    assert cursor == "b"
    cursor = await reconcile_payments_once(session_factory=factory, client=fake, limit=2, after_id=cursor)
    assert cursor is None
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("1500")
    payment = await db_session.get(PaymentInvoice, "c")
    assert payment.status == "credited"
    assert payment.was_expired_when_paid
    await db_session.commit()
    await reconcile_payments_once(session_factory=factory, client=fake, limit=20)
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("1500")


async def test_recovery_finds_paid_invoice_after_creation_response_was_lost(db_session):
    from app.payments.reconciliation import reconcile_payments_once

    partner = Partner(telegram_id="unknown", company_name="Recovery", project_name="test")
    db_session.add(partner)
    await db_session.flush()
    payment = PaymentInvoice(
        partner_id=partner.id, idempotency_key="unknown", status="creation_unknown",
        requested_rub=Decimal("1500"),
    )
    db_session.add(payment)
    await db_session.flush()
    fake = FakeCryptoPayClient()
    invoice = await fake.create_rub_invoice(amount_rub="1500", payload=payment.id)
    fake.invoices[invoice.invoice_id] = replace(invoice, status="paid", paid_asset="USDT", paid_amount="15")
    await db_session.commit()
    await reconcile_payments_once(
        session_factory=async_sessionmaker(db_session.bind, expire_on_commit=False), client=fake,
    )
    await db_session.refresh(payment)
    assert payment.status == "credited"
    assert payment.provider_invoice_id == invoice.invoice_id
    assert len(fake.created) == 1
