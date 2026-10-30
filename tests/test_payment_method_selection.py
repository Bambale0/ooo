from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.infrastructure.config import get_settings
from app.payments.models import PaymentInvoice
from app.payments.reconciliation import reconcile_lava_top_payments_once
from app.telegram.models import BotAction, BotDialog, BotNotification
from tests.test_crypto_payments import FakeCryptoPayClient
from tests.test_lava_top_payments import FakeLavaTopClient
from tests.test_telegram_cabinet import cabinet as cabinet_fixture
from tests.test_telegram_cabinet import partner

cabinet = cabinet_fixture


@pytest.fixture
def both_methods(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "payment_provider", "lava_top")
    monkeypatch.setattr(settings, "lava_top_api_key", SecretStr("test-lava-key"))
    monkeypatch.setattr(settings, "lava_top_offer_id", "11111111-1111-4111-8111-111111111111")
    monkeypatch.setattr(settings, "lava_top_webhook_secret", SecretStr("test-lava-webhook"))
    monkeypatch.setattr(settings, "crypto_pay_api_token", "test-crypto-pay-token")
    crypto, lava = FakeCryptoPayClient(), FakeLavaTopClient()
    monkeypatch.setattr("app.payments.crypto_pay.get_crypto_pay_client", lambda: crypto)
    monkeypatch.setattr("app.payments.lava_top.get_lava_top_client", lambda: lava)
    return crypto, lava


async def test_both_configured_methods_are_visible(cabinet, db_session, both_methods):
    feed, _ = cabinet
    await partner(db_session)
    screen = (await feed(callback="topup"))[-1]
    callbacks = {b.callback_data for row in screen.reply_markup.inline_keyboard for b in row}
    assert {"topup_method:lava_top", "topup_method:crypto_pay"} <= callbacks
    balance = (await feed(callback="balance"))[-1]
    assert "Крипто Бот" in balance.text and "СБП" in balance.text


@pytest.mark.parametrize("method", ["crypto_pay", "lava_top"])
async def test_selected_provider_survives_restart_default_change_and_replay(
    cabinet, db_session, both_methods, monkeypatch, method,
):
    feed, _ = cabinet
    owner = await partner(db_session)
    crypto, lava = both_methods
    screen = (await feed(callback=f"topup_method:{method}"))[-1]
    if method == "lava_top":
        assert "email" in screen.text.lower()
        await feed(text="buyer@example.org")
    else:
        assert "email" not in screen.text.lower()
    screen = (await feed(text="3000"))[-1]
    assert ("3,300" if method == "lava_top" else "3,000") in screen.text
    action = (await db_session.scalars(select(BotAction))).one()
    assert action.payload["payment_provider"] == method
    assert not crypto.created and not lava.created
    monkeypatch.setattr(get_settings(), "payment_provider", "crypto_pay" if method == "lava_top" else "lava_top")
    await feed(callback=f"confirm:{action.id}")
    await feed(callback=f"confirm:{action.id}")
    payment = (await db_session.scalars(select(PaymentInvoice))).one()
    assert payment.provider == method
    assert payment.requested_rub == Decimal("3000")
    assert payment.amount_due_rub == Decimal("3300" if method == "lava_top" else "3000")
    assert payment.invoice_url
    assert len(crypto.created) == (1 if method == "crypto_pay" else 0)
    assert len(lava.created) == (1 if method == "lava_top" else 0)
    assert owner.balance_rub == Decimal("1000")


async def test_unconfigured_explicit_method_does_not_silently_fallback(
    cabinet, db_session, both_methods, monkeypatch,
):
    feed, _ = cabinet
    await partner(db_session)
    monkeypatch.setattr(get_settings(), "crypto_pay_api_token", None)
    screen = (await feed(callback="topup_method:crypto_pay"))[-1]
    assert "недоступно" in screen.text.lower()
    assert await db_session.scalar(select(func.count()).select_from(BotAction)) == 0
    assert not both_methods[0].created and not both_methods[1].created


async def test_selected_confirmation_is_tenant_bound(cabinet, db_session, both_methods):
    feed, _ = cabinet
    await partner(db_session)
    await partner(db_session, "456")
    await feed(callback="topup_method:crypto_pay")
    await feed(text="3000")
    action = (await db_session.scalars(select(BotAction))).one()
    await feed(user=456, callback=f"confirm:{action.id}")
    assert not both_methods[0].created
    assert action.status == "pending"


async def test_selected_topup_cancel_never_creates_invoice(cabinet, db_session, both_methods):
    feed, _ = cabinet
    await partner(db_session)
    await feed(callback="topup_method:crypto_pay")
    await feed(text="/cancel")
    assert (await db_session.get(BotDialog, "123")).state == "menu"
    assert await db_session.scalar(select(func.count()).select_from(PaymentInvoice)) == 0


async def _stale_payment(db, owner, **values):
    row = PaymentInvoice(
        partner_id=owner.id, provider="lava_top", requested_rub=Decimal("3000"),
        provider_amount_rub=Decimal("3300"), buyer_email="buyer@example.org",
        idempotency_key=f"stale-{uuid4()}", status="creating",
        creation_claimed_until=datetime.now(UTC) - timedelta(minutes=2),
    )
    for name, value in values.items():
        setattr(row, name, value)
    db.add(row)
    await db.commit()
    return row


async def test_refresh_stale_creation_reports_unknown_without_resubmission(cabinet, db_session, both_methods):
    feed, _ = cabinet
    owner = await partner(db_session)
    row = await _stale_payment(db_session, owner)
    screen = (await feed(callback=f"payment:{row.id}"))[-1]
    assert row.status == "creation_unknown"
    assert "не подтверждено" in screen.text.lower()
    assert row.creation_claimed_until is None
    assert row.reconciliation_snapshot["result"] == "creation_lease_expired"
    await feed(callback=f"payment:{row.id}")
    assert not both_methods[0].created and not both_methods[1].created
    assert await db_session.scalar(select(func.count()).select_from(PaymentInvoice)) == 1
    assert owner.balance_rub == Decimal("1000")
    notifications = await db_session.scalar(select(func.count()).select_from(BotNotification))
    assert notifications == 1


async def test_background_sweep_expires_only_stale_unconfirmed_lava_creations(db_session, both_methods):
    owner = await partner(db_session)
    stale = await _stale_payment(db_session, owner)
    active = await _stale_payment(db_session, owner, creation_claimed_until=datetime.now(UTC) + timedelta(minutes=2))
    credited = await _stale_payment(db_session, owner, status="credited", credited_at=datetime.now(UTC))
    crypto = await _stale_payment(db_session, owner, provider="crypto_pay")
    await reconcile_lava_top_payments_once(
        session_factory=async_sessionmaker(db_session.bind, expire_on_commit=False), client=both_methods[1],
    )
    for row in (stale, active, credited, crypto):
        await db_session.refresh(row)
    assert stale.status == "creation_unknown"
    assert active.status == "creating"
    assert credited.status == "credited"
    assert crypto.status == "creating"
    assert not both_methods[0].created and not both_methods[1].created
