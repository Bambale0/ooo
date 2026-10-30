"""Exact provider-echoed correlation, never a heuristic amount/email match."""

from dataclasses import asdict
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.billing.models import LedgerEntry
from app.payments.lava_top import LavaTopClient, LavaTopError
from app.payments.reconciliation import reconcile_lava_top_payments_once
from tests.test_reconciliation_release import lava_fixture

LOCAL_ID = "11111111-1111-4111-8111-111111111111"
CONTRACT_ID = "22222222-2222-4222-8222-222222222222"


def provider_invoice(payment_id=LOCAL_ID, *, contract_id=CONTRACT_ID, email="buyer@example.org"):
    return {
        "id": contract_id,
        "status": "COMPLETED",
        "receipt": {"amount": "3300", "currency": "RUB"},
        "buyer": {"email": email},
        "datetime": "2026-10-30T12:00:00Z",
        "clientUtm": {"utm_source": "neironych", "utm_medium": "api_topup", "utm_content": payment_id},
    }


def make_client(http):
    return LavaTopClient(
        api_key="test-lava-key", offer_id=str(uuid4()), base_url="https://lava.invalid", timeout_seconds=1, client=http
    )


async def test_create_carries_exact_local_payment_id_in_supported_utm_fields():
    calls = []

    def response(request):
        import json

        calls.append(request)
        body = json.loads(request.content)
        assert body["clientUtm"] == {"utm_source": "neironych", "utm_medium": "api_topup", "utm_content": LOCAL_ID}
        return httpx.Response(
            201, json={"id": CONTRACT_ID, "status": "new", "amountTotal": {"amount": 3300, "currency": "RUB"}}
        )

    async with httpx.AsyncClient(base_url="https://lava.invalid", transport=httpx.MockTransport(response)) as http:
        await make_client(http).create_sbp_invoice(
            amount_rub=3300, buyer_email="buyer@example.org", payment_id=LOCAL_ID
        )
    assert len(calls) == 1


async def test_lookup_rechecks_exact_match_by_contract_id_and_requests_all_states():
    calls = []

    def response(request):
        calls.append(request)
        assert request.method == "GET"
        if request.url.path == "/api/v1/invoices":
            assert set(request.url.params.get_list("invoiceStatuses")) == {"NEW", "IN_PROGRESS", "COMPLETED", "FAILED"}
            return httpx.Response(200, json={"items": [provider_invoice()], "page": 1, "size": 100, "total": 1})
        assert request.url.path == f"/api/v1/invoices/{CONTRACT_ID}"
        return httpx.Response(200, json=provider_invoice())

    async with httpx.AsyncClient(base_url="https://lava.invalid", transport=httpx.MockTransport(response)) as http:
        found = await make_client(http).find_invoice_by_payment_id(LOCAL_ID, buyer_email="buyer@example.org")
    assert found.invoice_id == CONTRACT_ID and found.correlation_id == LOCAL_ID
    assert len(calls) == 2


async def test_same_amount_and_email_without_exact_correlation_never_match():
    item = provider_invoice(str(uuid4()))
    async with httpx.AsyncClient(
        base_url="https://lava.invalid",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"items": [item], "page": 1, "size": 100, "total": 1})
        ),
    ) as http:
        assert await make_client(http).find_invoice_by_payment_id(LOCAL_ID, buyer_email="buyer@example.org") is None


@pytest.mark.parametrize("failure", ["duplicate", "truncated", "wrong_email", "changed_identity", "malformed"])
async def test_ambiguous_or_unverified_recovery_fails_closed(failure):
    def response(request):
        if request.url.path != "/api/v1/invoices":
            return httpx.Response(200, json=provider_invoice(str(uuid4())))
        items = [provider_invoice(email="other@example.org" if failure == "wrong_email" else "buyer@example.org")]
        if failure == "duplicate":
            items.append(provider_invoice(contract_id=str(uuid4())))
        total = 100000 if failure == "truncated" else len(items)
        data = {"items": items, "page": 1, "size": 100, "total": total}
        if failure == "malformed":
            data["items"] = "not a list"
        return httpx.Response(200, json=data)

    async with httpx.AsyncClient(base_url="https://lava.invalid", transport=httpx.MockTransport(response)) as http:
        with pytest.raises(LavaTopError):
            await make_client(http).find_invoice_by_payment_id(LOCAL_ID, buyer_email="buyer@example.org")


async def test_worker_recovers_correlated_lost_invoice_and_credits_only_once(db_session):
    partner, payment, fake, invoice = await lava_fixture(db_session)
    payment.reconciliation_snapshot = {**payment.reconciliation_snapshot, "creation_correlation_id": payment.id}
    found = SimpleNamespace(**{**asdict(invoice), "status": "paid", "correlation_id": payment.id})
    fake.invoices[invoice.invoice_id] = found

    async def find(payment_id, *, buyer_email):
        assert payment_id == payment.id and buyer_email == payment.buyer_email
        return found

    fake.find_invoice_by_payment_id = find
    await db_session.commit()
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    for _ in range(2):
        await reconcile_lava_top_payments_once(session_factory=factory, client=fake)
    await db_session.refresh(payment)
    await db_session.refresh(partner)
    assert payment.status == "credited" and payment.provider_reference == invoice.invoice_id
    assert partner.balance_rub == Decimal("3000")
    assert await db_session.scalar(select(func.count()).select_from(LedgerEntry)) == 1
    assert len(fake.created) == 1


async def test_legacy_unknown_without_correlation_is_not_fuzzily_bound(db_session):
    _, payment, fake, _ = await lava_fixture(db_session)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Legacy invoices need explicit reconciliation evidence")

    fake.find_invoice_by_payment_id = forbidden
    await reconcile_lava_top_payments_once(
        session_factory=async_sessionmaker(db_session.bind, expire_on_commit=False), client=fake
    )
    await db_session.refresh(payment)
    assert payment.status == "creation_unknown" and payment.provider_reference is None
