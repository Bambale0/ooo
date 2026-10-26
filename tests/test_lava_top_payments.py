import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.billing.models import CoverageLedgerEntry, LedgerEntry
from app.infrastructure.config import get_settings
from app.payments.lava_top import LavaTopClient, LavaTopError, LavaTopInvoice
from app.payments.models import LavaTopWebhookEvent, PaymentInvoice
from app.payments.service import create_or_resume_lava_invoice
from tests.test_crypto_payments import _partner_with_api_key


class FakeLavaTopClient:
    def __init__(self) -> None:
        self.created: list[tuple[int, str]] = []
        self.invoices: dict[str, LavaTopInvoice] = {}
        self.next_id = 1

    async def create_sbp_invoice(self, *, amount_rub: int, buyer_email: str) -> LavaTopInvoice:
        self.created.append((amount_rub, buyer_email))
        invoice_id = f"11111111-1111-4111-8111-{self.next_id:012d}"
        invoice = LavaTopInvoice(
            invoice_id=invoice_id,
            status="active",
            amount=str(amount_rub),
            currency="RUB",
            payment_url=f"https://app.lava.top/invoice/{invoice_id}",
            paid_at=None,
            buyer_email=buyer_email,
        )
        self.invoices[invoice_id] = invoice
        self.next_id += 1
        return invoice

    async def get_invoice(self, invoice_id: str) -> LavaTopInvoice | None:
        return self.invoices.get(invoice_id)


def test_partner_payment_email_migration_backfills_latest_invoice_and_rolls_back():
    path = Path(__file__).parents[1] / "alembic/versions/20261026_0034_partner_payment_email.py"
    spec = spec_from_file_location("partner_payment_email_migration", path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE partners (id VARCHAR(36) PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE payment_invoices ("
                "id VARCHAR(36) PRIMARY KEY, partner_id VARCHAR(36), buyer_email VARCHAR(320), created_at DATETIME)"
            )
        )
        connection.execute(text("INSERT INTO partners (id) VALUES ('p1'), ('p2')"))
        connection.execute(
            text(
                "INSERT INTO payment_invoices (id, partner_id, buyer_email, created_at) VALUES "
                "('i1', 'p1', 'OLD@EXAMPLE.ORG', '2026-10-25 10:00:00'), "
                "('i2', 'p1', 'New@Example.org', '2026-10-26 10:00:00'), "
                "('i3', 'p2', NULL, '2026-10-26 11:00:00')"
            )
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert connection.execute(text("SELECT payment_email FROM partners WHERE id = 'p1'")).scalar_one() == (
                "new@example.org"
            )
            assert connection.execute(text("SELECT payment_email FROM partners WHERE id = 'p2'")).scalar_one() is None
            migration.downgrade()
        assert "payment_email" not in {column["name"] for column in inspect(connection).get_columns("partners")}
    engine.dispose()


async def test_lava_top_client_sends_exact_sbp_contract_and_parses_confirmation():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "id": "11111111-1111-4111-8111-111111111111",
                    "status": "NEW",
                    "amountTotal": {"amount": 1500, "currency": "RUB"},
                    "paymentUrl": "https://app.lava.top/pay/one",
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "11111111-1111-4111-8111-111111111111",
                "status": "COMPLETED",
                "receipt": {"amount": 1500, "currency": "RUB", "fee": 15},
                "buyer": {"email": "buyer@example.org"},
                "datetime": "2026-10-26T12:00:00Z",
            },
        )

    http = httpx.AsyncClient(base_url="https://gate.lava.top", transport=httpx.MockTransport(handler))
    client = LavaTopClient(
        api_key="lava-secret",
        offer_id="22222222-2222-4222-8222-222222222222",
        base_url="https://gate.lava.top",
        timeout_seconds=10,
        successful_return_url="https://example.org/payment/success",
        failure_return_url="https://example.org/payment/failure",
        cancel_return_url="https://example.org/payment/cancel",
        client=http,
    )
    created = await client.create_sbp_invoice(amount_rub=1500, buyer_email="buyer@example.org")
    confirmed = await client.get_invoice(created.invoice_id)
    await http.aclose()

    assert created.status == "active" and created.payment_url == "https://app.lava.top/pay/one"
    assert confirmed is not None and confirmed.status == "paid"
    assert confirmed.buyer_email == "buyer@example.org"
    assert requests[0].headers["x-api-key"] == "lava-secret"
    assert json.loads(requests[0].content) == {
        "email": "buyer@example.org",
        "offerId": "22222222-2222-4222-8222-222222222222",
        "currency": "RUB",
        "amount": 1500,
        "paymentProvider": "PAY2ME",
        "paymentMethod": "SBP",
        "successful_return_url": "https://example.org/payment/success",
        "failure_return_url": "https://example.org/payment/failure",
        "cancel_return_url": "https://example.org/payment/cancel",
    }


async def test_lava_top_sbp_webhook_rechecks_and_credits_exactly_once(
    client,
    db_session,
    monkeypatch,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "payment_provider", "lava_top")
    monkeypatch.setattr(settings, "lava_top_webhook_secret", SecretStr("test-lava-webhook-secret"))
    fake = FakeLavaTopClient()
    monkeypatch.setattr("app.payments.router.get_lava_top_client", lambda: fake)

    partner, token = await _partner_with_api_key(db_session, telegram_id="lava-partner")
    headers = {"Authorization": f"Bearer {token}"}
    missing_email = await client.post(
        "/api/v1/payments/invoices",
        headers=headers,
        json={"requested_rub": 1500, "idempotency_key": "lava-create-no-email"},
    )
    assert missing_email.status_code == 422
    assert missing_email.json()["detail"] == "buyer_email_required"

    created = await client.post(
        "/api/v1/payments/invoices",
        headers=headers,
        json={
            "requested_rub": 1500,
            "idempotency_key": "lava-create-idempotent",
            "buyer_email": "Buyer@Example.org",
        },
    )
    assert created.status_code == 202
    assert created.json()["provider"] == "lava_top"
    assert created.json()["accepted_assets"] == "RUB:SBP"
    assert fake.created == [(1500, "buyer@example.org")]

    duplicate = await client.post(
        "/api/v1/payments/invoices",
        headers=headers,
        json={
            "requested_rub": 1500,
            "idempotency_key": "lava-create-idempotent",
            "buyer_email": "buyer@example.org",
        },
    )
    assert duplicate.status_code == 202
    assert duplicate.json()["id"] == created.json()["id"]
    assert len(fake.created) == 1

    payment = await db_session.get(PaymentInvoice, created.json()["id"])
    provider_reference = payment.provider_reference
    fake.invoices[provider_reference] = replace(
        fake.invoices[provider_reference],
        status="paid",
        paid_at=datetime.now(UTC).isoformat(),
    )
    webhook_body = json.dumps(
        {
            "eventType": "payment.success",
            "contractId": provider_reference,
            "amount": 1,
            "currency": "USD",
            "timestamp": "2026-10-26T12:00:00Z",
        },
        separators=(",", ":"),
    ).encode()
    unauthorized = await client.post(
        "/api/v1/payments/lava-top/webhook",
        content=webhook_body,
        headers={"x-api-key": "wrong"},
    )
    assert unauthorized.status_code == 401

    webhook = await client.post(
        "/api/v1/payments/lava-top/webhook",
        content=webhook_body,
        headers={"x-api-key": "test-lava-webhook-secret"},
    )
    assert webhook.status_code == 200
    replay = await client.post(
        "/api/v1/payments/lava-top/webhook",
        content=webhook_body,
        headers={"x-api-key": "test-lava-webhook-secret"},
    )
    assert replay.status_code == 200

    await db_session.refresh(payment)
    await db_session.refresh(partner)
    assert payment.status == "credited"
    assert payment.paid_asset == "RUB"
    assert payment.paid_amount == Decimal("1500")
    assert payment.paid_usd_rate == Decimal("0.01")
    assert partner.balance_rub == Decimal("1500")
    assert partner.cost_coverage_rub == Decimal("1500")
    assert await db_session.scalar(select(func.count()).select_from(LedgerEntry)) == 1
    assert await db_session.scalar(select(func.count()).select_from(CoverageLedgerEntry)) == 1
    assert await db_session.scalar(select(func.count()).select_from(LavaTopWebhookEvent)) == 1


async def test_lava_top_reuses_and_updates_partner_payment_email(client, db_session, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "payment_provider", "lava_top")
    fake = FakeLavaTopClient()
    monkeypatch.setattr("app.payments.router.get_lava_top_client", lambda: fake)

    partner, token = await _partner_with_api_key(db_session, telegram_id="lava-saved-email")
    headers = {"Authorization": f"Bearer {token}"}
    first = await client.post(
        "/api/v1/payments/invoices",
        headers=headers,
        json={
            "requested_rub": 1500,
            "idempotency_key": "lava-save-email-first",
            "buyer_email": "Buyer@Example.org",
        },
    )
    assert first.status_code == 202
    await db_session.refresh(partner)
    assert partner.payment_email == "buyer@example.org"

    reused = await client.post(
        "/api/v1/payments/invoices",
        headers=headers,
        json={"requested_rub": 1600, "idempotency_key": "lava-save-email-reused"},
    )
    assert reused.status_code == 202
    assert fake.created[-1] == (1600, "buyer@example.org")

    updated = await client.post(
        "/api/v1/payments/invoices",
        headers=headers,
        json={
            "requested_rub": 1700,
            "idempotency_key": "lava-save-email-updated",
            "buyer_email": "new@example.org",
        },
    )
    assert updated.status_code == 202
    await db_session.refresh(partner)
    assert partner.payment_email == "new@example.org"


async def test_lava_top_unknown_create_is_not_replayed(db_session):
    partner, _ = await _partner_with_api_key(db_session, telegram_id="lava-unknown")

    class UnknownClient(FakeLavaTopClient):
        async def create_sbp_invoice(self, *, amount_rub: int, buyer_email: str) -> LavaTopInvoice:
            self.created.append((amount_rub, buyer_email))
            raise LavaTopError("connection_lost_after_submission")

    fake = UnknownClient()
    with pytest.raises(HTTPException) as failed:
        await create_or_resume_lava_invoice(
            db_session,
            partner=partner,
            requested_rub=1500,
            idempotency_key="lava-unknown-create",
            buyer_email="buyer@example.org",
            client=fake,
        )
    assert failed.value.status_code == 503
    repeated = await create_or_resume_lava_invoice(
        db_session,
        partner=partner,
        requested_rub=1500,
        idempotency_key="lava-unknown-create",
        buyer_email="buyer@example.org",
        client=fake,
    )
    assert repeated.status == "creation_unknown"
    assert fake.created == [(1500, "buyer@example.org")]


async def test_lava_top_reconciliation_recovers_lost_webhook(db_session):
    from app.payments.reconciliation import reconcile_lava_top_payments_once

    partner, _ = await _partner_with_api_key(db_session, telegram_id="lava-reconcile")
    fake = FakeLavaTopClient()
    invoice = await fake.create_sbp_invoice(amount_rub=1500, buyer_email="buyer@example.org")
    fake.invoices[invoice.invoice_id] = replace(
        invoice,
        status="paid",
        paid_at=datetime.now(UTC).isoformat(),
    )
    payment = PaymentInvoice(
        partner_id=partner.id,
        provider="lava_top",
        provider_reference=invoice.invoice_id,
        idempotency_key="lava-background-reconcile",
        requested_rub=Decimal("1500"),
        accepted_assets="RUB:SBP",
        buyer_email="buyer@example.org",
        status="active",
    )
    db_session.add(payment)
    await db_session.commit()

    await reconcile_lava_top_payments_once(
        session_factory=async_sessionmaker(db_session.bind, expire_on_commit=False),
        client=fake,
    )
    await db_session.refresh(payment)
    await db_session.refresh(partner)
    assert payment.status == "credited"
    assert partner.balance_rub == Decimal("1500")

    await reconcile_lava_top_payments_once(
        session_factory=async_sessionmaker(db_session.bind, expire_on_commit=False),
        client=fake,
    )
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("1500")


async def test_lava_top_webhook_for_unrelated_sale_is_acknowledged(client, db_session, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "lava_top_webhook_secret", SecretStr("test-lava-webhook-secret"))
    fake = FakeLavaTopClient()
    monkeypatch.setattr("app.payments.router.get_lava_top_client", lambda: fake)
    body = json.dumps(
        {
            "eventType": "payment.success",
            "contractId": "33333333-3333-4333-8333-333333333333",
            "timestamp": "2026-10-26T12:01:00Z",
        },
        separators=(",", ":"),
    ).encode()
    response = await client.post(
        "/api/v1/payments/lava-top/webhook",
        content=body,
        headers={"x-api-key": "test-lava-webhook-secret"},
    )
    assert response.status_code == 200
    refund_body = json.dumps(
        {
            "event_id": "44444444-4444-4444-8444-444444444444",
            "event_type": "refund.success",
            "created_at": "2026-10-26T12:02:00Z",
            "data": {},
        },
        separators=(",", ":"),
    ).encode()
    refund = await client.post(
        "/api/v1/payments/lava-top/webhook",
        content=refund_body,
        headers={"x-api-key": "test-lava-webhook-secret"},
    )
    assert refund.status_code == 200
    events = (
        await db_session.execute(select(LavaTopWebhookEvent).order_by(LavaTopWebhookEvent.created_at))
    ).scalars().all()
    assert events[0].provider_reference == "33333333-3333-4333-8333-333333333333"
    assert events[1].event_type == "refund.success" and events[1].provider_reference is None
