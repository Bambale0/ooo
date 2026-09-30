import asyncio
import json
import os
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.accounts.models import Partner
from app.billing.models import CoverageLedgerEntry, LedgerEntry
from app.payments.models import CryptoPayWebhookEvent, PaymentInvoice
from app.payments.router import crypto_pay_webhook
from app.payments.service import apply_paid_provider_invoice
from app.telegram.models import BotNotification
from tests.test_crypto_payments import FakeCryptoPayClient, _crypto_pay_signature


@pytest.mark.integration
async def test_postgres_webhook_duplicates_and_recovery_credit_exactly_once(monkeypatch):
    url = os.getenv("TEST_POSTGRES_DATABASE_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_DATABASE_URL is not configured")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    fake = FakeCryptoPayClient()
    fake.next_id = uuid4().int % (2**62)
    monkeypatch.setattr("app.payments.router.get_crypto_pay_client", lambda: fake)
    partner_id = payment_id = None
    update_id = uuid4().int % (2**62)
    try:
        async with factory() as db:
            partner = Partner(telegram_id=f"concurrent-{uuid4()}", company_name="Test", project_name="Test")
            db.add(partner)
            await db.flush()
            partner_id = partner.id
            payment = PaymentInvoice(
                partner_id=partner_id, status="active", requested_rub=Decimal("1500"),
                idempotency_key=str(uuid4()),
            )
            db.add(payment)
            await db.flush()
            payment_id = payment.id
            invoice = await fake.create_rub_invoice(amount_rub="1500", payload=payment_id)
            payment.provider_invoice_id = invoice.invoice_id
            invoice = replace(invoice, status="paid", paid_asset="TON", paid_amount="3.123456")
            fake.invoices[invoice.invoice_id] = invoice
            await db.commit()

        async def deliver(event_id):
            body = json.dumps({
                "update_id": event_id, "update_type": "invoice_paid",
                "payload": {"invoice_id": invoice.invoice_id},
            }).encode()

            async def receive():
                return {"type": "http.request", "body": body}

            async with factory() as db:
                # Load a stale object before competing transactions: locked reads must refresh it.
                stale = await db.get(PaymentInvoice, payment_id)
                assert stale is not None
                await asyncio.sleep(0)
                result = await crypto_pay_webhook(
                    Request({"type": "http"}, receive), db,
                    crypto_pay_api_signature=_crypto_pay_signature(body),
                )
                await db.commit()
                return result

        async def recover():
            async with factory() as db:
                await apply_paid_provider_invoice(db, provider_invoice=invoice)
                await db.commit()

        results = await asyncio.wait_for(
            asyncio.gather(deliver(update_id), deliver(update_id), deliver(update_id + 1), recover()),
            timeout=20,
        )
        assert results[:3] == [{"ok": True}] * 3
        async with factory() as db:
            partner = await db.get(Partner, partner_id)
            assert partner.balance_rub == Decimal("1500")
            assert partner.cost_coverage_rub == Decimal("1500")
            payment = await db.get(PaymentInvoice, payment_id)
            assert payment.status == "credited"
            assert payment.paid_amount == Decimal("3.123456")
            for model in (LedgerEntry, CoverageLedgerEntry):
                assert await db.scalar(select(func.count()).select_from(model).where(
                    model.partner_id == partner_id,
                )) == 1
            assert await db.scalar(select(func.count()).select_from(CryptoPayWebhookEvent).where(
                CryptoPayWebhookEvent.update_id.in_((update_id, update_id + 1)),
            )) == 2
    finally:
        async with factory() as db:
            await db.execute(delete(CryptoPayWebhookEvent).where(
                CryptoPayWebhookEvent.update_id.in_((update_id, update_id + 1)),
            ))
            if payment_id:
                await db.execute(delete(BotNotification).where(BotNotification.dedupe_key.in_((
                    f"payment-credited:{payment_id}", f"payment-paid:{payment_id}",
                ))))
                for model in (LedgerEntry, CoverageLedgerEntry):
                    await db.execute(delete(model).where(model.partner_id == partner_id))
                await db.execute(delete(PaymentInvoice).where(PaymentInvoice.id == payment_id))
            if partner_id:
                await db.execute(delete(Partner).where(Partner.id == partner_id))
            await db.commit()
        await engine.dispose()
