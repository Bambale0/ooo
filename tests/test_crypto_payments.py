import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.accounts.models import ApiKey, Partner
from app.billing.models import CoverageLedgerEntry, LedgerEntry
from app.infrastructure.security import hash_secret
from app.payments.crypto_pay import CryptoPayInvoice
from app.payments.models import CryptoPayWebhookEvent, PaymentInvoice, PaymentRefund


class FakeCryptoPayClient:
    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []
        self.deleted: list[int] = []
        self.invoices: dict[int, CryptoPayInvoice] = {}
        self.next_id = 7001

    async def create_rub_invoice(self, *, amount_rub: str, payload: str) -> CryptoPayInvoice:
        self.created.append((amount_rub, payload))
        invoice = CryptoPayInvoice(
            invoice_id=self.next_id,
            status="active",
            amount=amount_rub,
            bot_invoice_url=f"https://t.me/CryptoBot?start=IV{self.next_id}",
            payload=payload,
            expiration_date=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            paid_at=None,
            paid_asset=None,
            paid_amount=None,
            paid_fiat_rate=None,
            paid_usd_rate=None,
        )
        self.invoices[invoice.invoice_id] = invoice
        self.next_id += 1
        return invoice

    async def get_invoice(self, invoice_id: int) -> CryptoPayInvoice | None:
        return self.invoices.get(invoice_id)

    async def find_invoice_by_payload(self, payload: str) -> CryptoPayInvoice | None:
        for invoice in self.invoices.values():
            if invoice.payload == payload:
                return invoice
        return None

    async def delete_invoice(self, invoice_id: int) -> None:
        self.deleted.append(invoice_id)


async def _partner_with_api_key(db_session, *, telegram_id: str) -> tuple[Partner, str]:
    partner = Partner(
        telegram_id=telegram_id,
        company_name="Payment Partner",
        project_name="Payment Bot",
    )
    db_session.add(partner)
    await db_session.flush()
    token = f"nrn_{telegram_id}_payment_key"
    db_session.add(
        ApiKey(
            partner_id=partner.id,
            name="payments",
            key_hash=hash_secret(token),
            key_prefix=token[:8],
            is_active=True,
        )
    )
    await db_session.commit()
    return partner, token


def _crypto_pay_signature(raw_body: bytes, token: str = "test-crypto-pay-token") -> str:
    secret = hashlib.sha256(token.encode("utf-8")).digest()
    return hmac.new(secret, raw_body, hashlib.sha256).hexdigest()


async def test_crypto_pay_paid_invoice_requires_manual_credit_and_credits_both_ledgers(
    client,
    db_session,
    admin_headers,
    monkeypatch,
):
    fake = FakeCryptoPayClient()
    monkeypatch.setattr("app.payments.router.get_crypto_pay_client", lambda: fake)

    partner, token = await _partner_with_api_key(db_session, telegram_id="payment-flow")
    partner_id = partner.id
    headers = {"Authorization": f"Bearer {token}"}

    created = await client.post(
        "/api/v1/payments/invoices",
        headers=headers,
        json={"requested_rub": 1500, "idempotency_key": "payment-create-idem-1"},
    )
    assert created.status_code == 202
    payment_id = created.json()["id"]
    assert created.json()["status"] == "active"
    assert Decimal(created.json()["requested_rub"]) == Decimal("1500.00")
    assert created.json()["accepted_assets"] == "USDT,TON"
    assert len(fake.created) == 1

    duplicate = await client.post(
        "/api/v1/payments/invoices",
        headers=headers,
        json={"requested_rub": 1500, "idempotency_key": "payment-create-idem-1"},
    )
    assert duplicate.status_code == 202
    assert duplicate.json()["id"] == payment_id
    assert len(fake.created) == 1

    payment = await db_session.get(PaymentInvoice, payment_id)
    assert payment is not None
    provider_invoice_id = payment.provider_invoice_id
    assert provider_invoice_id is not None
    fake.invoices[provider_invoice_id] = CryptoPayInvoice(
        invoice_id=provider_invoice_id,
        status="paid",
        amount="1500",
        bot_invoice_url=payment.invoice_url or "",
        payload=payment.id,
        expiration_date=payment.expires_at.isoformat() if payment.expires_at else None,
        paid_at=datetime.now(UTC).isoformat(),
        paid_asset="USDT",
        paid_amount="17.250000",
        paid_fiat_rate="86.956521739130434783",
        paid_usd_rate="1.000000",
    )

    webhook_body = json.dumps(
        {
            "update_id": 90001,
            "update_type": "invoice_paid",
            "request_date": datetime.now(UTC).isoformat(),
            "payload": {"invoice_id": provider_invoice_id},
        },
        separators=(",", ":"),
    ).encode("utf-8")
    webhook_headers = {
        "content-type": "application/json",
        "crypto-pay-api-signature": _crypto_pay_signature(webhook_body),
    }
    webhook = await client.post(
        "/api/v1/payments/crypto-pay/webhook",
        content=webhook_body,
        headers=webhook_headers,
    )
    assert webhook.status_code == 200

    await db_session.refresh(partner)
    await db_session.refresh(payment)
    assert payment.status == "paid_waiting_credit"
    assert payment.paid_asset == "USDT"
    assert Decimal(payment.paid_amount) == Decimal("17.250000")
    assert Decimal(partner.balance_rub) == Decimal("0.00")
    assert Decimal(partner.cost_coverage_rub) == Decimal("0.00")

    duplicate_webhook = await client.post(
        "/api/v1/payments/crypto-pay/webhook",
        content=webhook_body,
        headers=webhook_headers,
    )
    assert duplicate_webhook.status_code == 200
    events = await db_session.execute(select(CryptoPayWebhookEvent))
    assert len(list(events.scalars().all())) == 1

    credited = await client.post(
        f"/api/v1/payments/invoices/{payment_id}/credit",
        headers=admin_headers,
    )
    assert credited.status_code == 200
    assert credited.json()["status"] == "credited"

    duplicate_credit = await client.post(
        f"/api/v1/payments/invoices/{payment_id}/credit",
        headers=admin_headers,
    )
    assert duplicate_credit.status_code == 200

    await db_session.refresh(partner)
    assert Decimal(partner.balance_rub) == Decimal("1500.00")
    assert Decimal(partner.cost_coverage_rub) == Decimal("1500.00")

    retail = await db_session.execute(
        select(LedgerEntry).where(LedgerEntry.partner_id == partner_id)
    )
    coverage = await db_session.execute(
        select(CoverageLedgerEntry).where(CoverageLedgerEntry.partner_id == partner_id)
    )
    assert [entry.operation_type for entry in retail.scalars().all()] == ["payment_credit"]
    assert [entry.operation_type for entry in coverage.scalars().all()] == ["payment_coverage_credit"]


async def test_confirmed_partial_refund_reverses_retail_and_coverage_idempotently(
    client,
    db_session,
    admin_headers,
):
    partner = Partner(
        telegram_id="payment-refund",
        company_name="Refund Partner",
        project_name="Refund Bot",
        balance_rub=Decimal("1500.00"),
        cost_coverage_rub=Decimal("1500.00"),
    )
    db_session.add(partner)
    await db_session.flush()
    payment = PaymentInvoice(
        partner_id=partner.id,
        provider_invoice_id=8001,
        idempotency_key="refund-payment-original",
        status="credited",
        requested_rub=Decimal("1500.00"),
        accepted_assets="USDT,TON",
        invoice_url="https://t.me/CryptoBot?start=IV8001",
        credited_at=datetime.now(UTC),
    )
    db_session.add(payment)
    await db_session.commit()
    payment_id = payment.id

    first = await client.post(
        f"/api/v1/payments/invoices/{payment_id}/refunds",
        headers=admin_headers,
        json={
            "amount_rub": "500.00",
            "idempotency_key": "confirmed-refund-1",
            "reason": "Confirmed external partial refund",
        },
    )
    assert first.status_code == 200
    assert first.json()["payment_status"] == "partially_refunded"
    assert Decimal(first.json()["refunded_rub_total"]) == Decimal("500.00")

    duplicate = await client.post(
        f"/api/v1/payments/invoices/{payment_id}/refunds",
        headers=admin_headers,
        json={
            "amount_rub": "500.00",
            "idempotency_key": "confirmed-refund-1",
            "reason": "Confirmed external partial refund",
        },
    )
    assert duplicate.status_code == 200

    await db_session.refresh(partner)
    assert Decimal(partner.balance_rub) == Decimal("1000.00")
    assert Decimal(partner.cost_coverage_rub) == Decimal("1000.00")

    refunds = await db_session.execute(select(PaymentRefund))
    assert len(list(refunds.scalars().all())) == 1


async def test_crypto_pay_webhook_rejects_invalid_signature(client, monkeypatch):
    fake = FakeCryptoPayClient()
    monkeypatch.setattr("app.payments.router.get_crypto_pay_client", lambda: fake)
    body = b'{"update_id":1,"update_type":"invoice_paid","payload":{"invoice_id":1}}'
    response = await client.post(
        "/api/v1/payments/crypto-pay/webhook",
        content=body,
        headers={"crypto-pay-api-signature": "invalid"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_crypto_pay_signature"
