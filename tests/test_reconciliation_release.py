"""Regressions from the October 30 production audit; no live provider calls."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.accounts.models import Partner
from app.billing.models import LedgerEntry
from app.infrastructure.config import get_settings
from app.infrastructure.retry import utc_now
from app.payments.models import PaymentInvoice
from app.payments.reconciliation import reconcile_payments_once
from app.payments.service import apply_paid_provider_invoice, sync_lava_provider_invoice
from app.referrals import models as referral_models  # noqa: F401 -- register fixture schema before create_all
from tests.test_crypto_payments import FakeCryptoPayClient
from tests.test_lava_top_payments import FakeLavaTopClient


async def crypto_fixture(db, *, status="expired", age_days=30, verified=True):
    partner = Partner(telegram_id="bounded-crypto", company_name="Test", project_name="Test")
    db.add(partner)
    await db.flush()
    fake = FakeCryptoPayClient()
    payment = PaymentInvoice(
        partner_id=partner.id,
        idempotency_key="bounded",
        requested_rub=Decimal("1500"),
        status=status,
        created_at=utc_now() - timedelta(days=age_days),
        expires_at=utc_now() - timedelta(days=age_days),
    )
    if verified:
        payment.reconciliation_snapshot = {"provider_status": "expired", "source": "provider_invoice_read"}
    db.add(payment)
    await db.flush()
    invoice = await fake.create_rub_invoice(amount_rub="1500", payload=payment.id)
    fake.invoices[invoice.invoice_id] = replace(invoice, status="expired")
    payment.provider_invoice_id = invoice.invoice_id
    await db.commit()
    fake.get_invoice = AsyncMock(wraps=fake.get_invoice)
    return partner, payment, fake, invoice


async def reconcile_crypto(db, fake):
    return await reconcile_payments_once(
        session_factory=async_sessionmaker(db.bind, expire_on_commit=False), client=fake
    )


async def test_old_provider_confirmed_expiry_does_not_poll_forever(db_session):
    _, payment, fake, _ = await crypto_fixture(db_session)
    await reconcile_crypto(db_session, fake)
    fake.get_invoice.assert_not_awaited()
    await db_session.refresh(payment)
    assert payment.status == "expired"
    assert await db_session.scalar(select(func.count()).select_from(LedgerEntry)) == 0


@pytest.mark.parametrize(
    "status,age,verified",
    [
        ("expired", 0, True),
        ("expired", 30, False),
        ("active", 30, True),
        ("creation_unknown", 30, True),
        ("paid_waiting_credit", 30, True),
    ],
)
async def test_unsettled_and_recent_invoices_remain_in_recovery(db_session, status, age, verified):
    _, _, fake, _ = await crypto_fixture(db_session, status=status, age_days=age, verified=verified)
    await reconcile_crypto(db_session, fake)
    fake.get_invoice.assert_awaited_once()


async def test_expiry_poll_window_is_configurable(db_session, monkeypatch):
    _, _, fake, _ = await crypto_fixture(db_session, age_days=4)
    monkeypatch.setattr(get_settings(), "payment_expired_recheck_seconds", 7 * 86400, raising=False)
    await reconcile_crypto(db_session, fake)
    fake.get_invoice.assert_awaited_once()


async def test_late_paid_confirmation_after_poll_window_still_credits_exactly_once(db_session):
    partner, payment, fake, invoice = await crypto_fixture(db_session)
    await reconcile_crypto(db_session, fake)
    paid = replace(invoice, status="paid", paid_asset="USDT", paid_amount="15")
    await apply_paid_provider_invoice(db_session, provider_invoice=paid)
    await db_session.commit()
    await apply_paid_provider_invoice(db_session, provider_invoice=paid)
    await db_session.commit()
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("1500")
    assert payment.was_expired_when_paid
    assert await db_session.scalar(select(func.count()).select_from(LedgerEntry)) == 1


async def lava_fixture(db):
    partner = Partner(telegram_id="lava-recovery-audit", company_name="Test", project_name="Test")
    db.add(partner)
    await db.flush()
    p = PaymentInvoice(
        partner_id=partner.id,
        provider="lava_top",
        idempotency_key="lost",
        requested_rub=Decimal("3000"),
        provider_amount_rub=Decimal("3300"),
        status="creation_unknown",
        buyer_email="buyer@example.org",
        reconciliation_snapshot={"source": "creation_claim_recovery", "result": "creation_lease_expired"},
    )
    db.add(p)
    await db.commit()
    fake = FakeLavaTopClient()
    invoice = await fake.create_sbp_invoice(amount_rub=3300, buyer_email=p.buyer_email)
    return partner, p, fake, invoice


async def test_lava_diagnostics_do_not_block_manual_reconciliation(client, db_session, monkeypatch, admin_headers):
    _, p, fake, invoice = await lava_fixture(db_session)
    fake.invoices[invoice.invoice_id] = replace(invoice, status="failed")
    monkeypatch.setattr("app.payments.router.get_lava_top_client", lambda: fake)
    body = {"provider_invoice_id": invoice.invoice_id, "reason": "Verified exact provider contract after lost response"}
    for _ in range(2):
        response = await client.post(f"/api/v1/payments/invoices/{p.id}/reconcile", headers=admin_headers, json=body)
        assert response.status_code == 200, response.text
    await db_session.refresh(p)
    assert p.status == "failed" and p.provider_reference == invoice.invoice_id
    assert p.reconciliation_snapshot["source"] == "creation_claim_recovery"
    assert p.reconciliation_snapshot["actor"] == "admin_api"
    assert await db_session.scalar(select(func.count()).select_from(LedgerEntry)) == 0


async def test_lava_contract_already_bound_elsewhere_cannot_reconcile_other_payment(db_session):
    partner, p, _, invoice = await lava_fixture(db_session)
    other = PaymentInvoice(
        partner_id=partner.id,
        provider="lava_top",
        idempotency_key="other",
        requested_rub=Decimal("3000"),
        provider_amount_rub=Decimal("3300"),
        buyer_email=p.buyer_email,
        provider_reference=invoice.invoice_id,
        status="active",
    )
    db_session.add(other)
    await db_session.commit()
    with pytest.raises(HTTPException) as exc:
        await sync_lava_provider_invoice(db_session, provider_invoice=invoice, payment_id=p.id)
    assert exc.value.detail == "payment_provider_id_conflict"
    assert p.provider_reference is None


async def test_lava_stale_failure_does_not_erase_verified_paid_state(db_session):
    _, p, _, invoice = await lava_fixture(db_session)
    p.status = "paid_waiting_credit"
    p.paid_at = utc_now()
    p.provider_reference = invoice.invoice_id
    await db_session.commit()
    await sync_lava_provider_invoice(db_session, provider_invoice=replace(invoice, status="failed"), payment_id=p.id)
    assert p.status == "paid_waiting_credit" and p.paid_at is not None
