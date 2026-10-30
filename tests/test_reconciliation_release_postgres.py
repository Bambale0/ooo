"""Real PostgreSQL races for the audit fixes; isolated CI database only."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from app.accounts.models import Partner
from app.billing.models import CoverageLedgerEntry, LedgerEntry
from app.generations.models import Generation
from app.generations.submission_recovery import recover_unconfirmed_submissions_once
from app.infrastructure.retry import utc_now
from app.payments.lava_top import LavaTopInvoice
from app.payments.models import PaymentInvoice
from app.payments.reconciliation import reconcile_lava_top_payments_once
from app.payments.service import sync_lava_provider_invoice
from app.providers.models import ProviderAttempt
from app.telegram.models import BotNotification
from app.webhooks.models import WebhookEvent
from tests.test_native_postgres_finality import cleanup, infrastructure, seed


@pytest.mark.integration
async def test_postgres_competing_deadline_sweepers_finalize_once_and_preserve_coverage():
    engine, factory = infrastructure()
    ids = await seed(factory)
    try:
        async with factory() as db:
            g = await db.get(Generation, ids[1])
            a = await db.get(ProviderAttempt, ids[2])
            g.mode = "videos/generations"
            g.model_slug = "seedance-2.5"
            g.status = a.status = "reconciliation_required"
            g.request_payload = {"partner_billing_policy": "actual_only", "protocol": "videos/generations"}
            g.created_at = a.created_at = utc_now() - timedelta(hours=1)
            a.cost_reserve_usdt = Decimal(".1")
            await db.commit()

        async def sweep():
            async with factory() as db:
                count = await recover_unconfirmed_submissions_once(db)
                await db.commit()
                return count

        counts = await asyncio.wait_for(asyncio.gather(*(sweep() for _ in range(6))), timeout=20)
        assert sum(counts) == 1
        async with factory() as db:
            g, a = await db.get(Generation, ids[1]), await db.get(ProviderAttempt, ids[2])
            partner = await db.get(Partner, ids[0])
            assert g.status == "failed" and g.billing_status == "not_chargeable"
            assert g.provider_cost_hold_usdt == Decimal(".1") and a.cost_status == "unknown"
            assert partner.balance_rub == Decimal("100") and partner.cost_coverage_rub == Decimal("90")
            assert (
                await db.scalar(
                    select(func.count()).select_from(WebhookEvent).where(WebhookEvent.generation_id == g.id)
                )
                == 1
            )
            assert (
                await db.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.generation_id == g.id))
                == 0
            )
    finally:
        await cleanup(factory, ids)
        await engine.dispose()


@pytest.mark.integration
async def test_postgres_lost_lava_recovery_racing_confirmations_credits_once():
    engine, factory = infrastructure()
    partner_id, payment_id = str(uuid4()), str(uuid4())
    invoice = LavaTopInvoice(
        invoice_id=str(uuid4()),
        status="paid",
        amount="3300",
        currency="RUB",
        buyer_email="isolated@example.org",
        paid_at=utc_now().isoformat(),
        payment_url=None,
        correlation_id=payment_id,
    )

    class Client:
        async def find_invoice_by_payment_id(self, local_id, *, buyer_email):
            assert local_id == payment_id and buyer_email == invoice.buyer_email
            await asyncio.sleep(0)
            return invoice

        async def get_invoice(self, provider_id):
            assert provider_id == invoice.invoice_id
            return invoice

    try:
        async with factory() as db:
            db.add(
                Partner(
                    id=partner_id, telegram_id="lava-pg-" + str(uuid4()), company_name="Isolated", project_name="Test"
                )
            )
            await db.flush()
            db.add(
                PaymentInvoice(
                    id=payment_id,
                    partner_id=partner_id,
                    provider="lava_top",
                    status="creation_unknown",
                    requested_rub=Decimal("3000"),
                    provider_amount_rub=Decimal("3300"),
                    idempotency_key=str(uuid4()),
                    buyer_email=invoice.buyer_email,
                    reconciliation_snapshot={"creation_correlation_id": payment_id},
                )
            )
            await db.commit()

        async def recover():
            await reconcile_lava_top_payments_once(session_factory=factory, client=Client())

        async def confirm():
            async with factory() as db:
                await sync_lava_provider_invoice(db, provider_invoice=invoice, payment_id=payment_id)
                await db.commit()

        await asyncio.wait_for(asyncio.gather(recover(), recover(), confirm(), confirm()), timeout=20)
        async with factory() as db:
            partner = await db.get(Partner, partner_id)
            payment = await db.get(PaymentInvoice, payment_id)
            assert partner.balance_rub == Decimal("3000") and payment.status == "credited"
            assert payment.provider_reference == invoice.invoice_id
            for model in (LedgerEntry, CoverageLedgerEntry):
                assert (
                    await db.scalar(select(func.count()).select_from(model).where(model.partner_id == partner_id)) == 1
                )
    finally:
        async with factory() as db:
            await db.execute(
                delete(BotNotification).where(
                    BotNotification.dedupe_key.in_((f"payment-credited:{payment_id}", f"payment-paid:{payment_id}"))
                )
            )
            for model in (LedgerEntry, CoverageLedgerEntry):
                await db.execute(delete(model).where(model.partner_id == partner_id))
            await db.execute(delete(PaymentInvoice).where(PaymentInvoice.id == payment_id))
            await db.execute(delete(Partner).where(Partner.id == partner_id))
            await db.commit()
        await engine.dispose()
