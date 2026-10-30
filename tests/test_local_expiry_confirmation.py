"""A local expiry is not provider proof until its successful read is persisted."""
from sqlalchemy import select

from app.billing.models import LedgerEntry
from tests.test_reconciliation_release import crypto_fixture, reconcile_crypto


async def test_old_locally_expired_invoice_records_provider_confirmation_then_stops_polling(db_session):
    _, payment, fake, _ = await crypto_fixture(db_session, age_days=30, verified=False)
    await reconcile_crypto(db_session, fake)
    await db_session.refresh(payment)
    assert payment.status == "expired"
    assert (payment.reconciliation_snapshot or {}).get("provider_status") == "expired"
    assert payment.reconciliation_snapshot["source"] == "provider_invoice_read"
    assert payment.paid_at is None and payment.credited_at is None
    await reconcile_crypto(db_session, fake)
    fake.get_invoice.assert_awaited_once()
    assert (await db_session.scalars(select(LedgerEntry))).all() == []
