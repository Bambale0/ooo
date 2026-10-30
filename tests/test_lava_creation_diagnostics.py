"""Creation failures remain inspectable without leaking payment data or replaying POST."""

import json
import logging
from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.billing.models import CoverageLedgerEntry, LedgerEntry
from app.payments.lava_top import LavaTopClient, LavaTopError
from app.payments.models import PaymentInvoice
from app.payments.service import create_or_resume_lava_invoice
from tests.test_crypto_payments import _partner_with_api_key


@pytest.mark.parametrize(
    ("failure", "error_code", "upstream_status"),
    [
        ("timeout", "ReadTimeout", None),
        ("http", "provider_http_503", 503),
        ("json", "invalid_provider_response", None),
        ("invoice", "invalid_lava_top_invoice", None),
        ("untrusted", "payment_provider_error", None),
    ],
)
async def test_lava_create_failure_persists_safe_diagnostics_without_retry_or_credit(
    db_session, caplog, failure, error_code, upstream_status,
):
    partner, _ = await _partner_with_api_key(db_session, telegram_id=f"lava-diagnostic-{failure}")
    buyer_email = "private-buyer@example.org"
    api_key = "fake-api-key-not-a-credential"
    private_url = "https://payments.example.org/pay?private_checkout=do-not-log"
    private_body = f"{buyer_email} {api_key} {private_url}"
    calls = []

    async def transport(request):
        calls.append(request.method)
        if failure == "timeout":
            raise httpx.ReadTimeout(private_body, request=request)
        if failure == "http":
            return httpx.Response(503, json={"error": private_body})
        if failure == "json":
            return httpx.Response(200, content=private_body)
        if failure == "invoice":
            return httpx.Response(200, json={"error": private_body})
        raise LavaTopError(private_body)

    caplog.set_level(logging.WARNING, logger="app.payments.service")
    async with httpx.AsyncClient(
        base_url="https://payments.example.org", transport=httpx.MockTransport(transport),
    ) as http:
        lava = LavaTopClient(
            api_key=api_key, offer_id="11111111-1111-4111-8111-111111111111",
            base_url="https://payments.example.org", timeout_seconds=10, client=http,
        )
        kwargs = dict(
            partner=partner, requested_rub=5000, idempotency_key=f"diagnostic-{failure}",
            buyer_email=buyer_email, client=lava,
        )
        with pytest.raises(HTTPException) as caught:
            await create_or_resume_lava_invoice(db_session, **kwargs)
        assert caught.value.status_code == 503
        assert caught.value.detail == "payment_provider_temporarily_unavailable"
        payment = await db_session.scalar(select(PaymentInvoice).where(
            PaymentInvoice.partner_id == partner.id,
        ))
        await db_session.rollback()
        await db_session.refresh(payment)
        await db_session.refresh(partner)

        snapshot = payment.reconciliation_snapshot
        assert snapshot is not None, "Creation failure discarded the provider error"
        assert snapshot["source"] == "create_error"
        assert snapshot["result"] == "provider_request_failed"
        assert snapshot["error_type"] == "LavaTopError"
        assert snapshot["error_code"] == error_code
        assert snapshot["upstream_status"] == upstream_status
        assert snapshot["elapsed_ms"] >= 0
        assert snapshot["observed_at"]
        assert payment.status == "creation_unknown"
        assert payment.provider_reference is None
        assert payment.creation_claimed_until is None
        assert payment.paid_at is None
        assert payment.credited_at is None
        assert payment.requested_rub == Decimal("5000")
        assert payment.provider_amount_rub == Decimal("5500")
        assert partner.balance_rub == Decimal("0")
        assert await db_session.scalar(select(func.count()).select_from(LedgerEntry)) == 0
        assert await db_session.scalar(select(func.count()).select_from(CoverageLedgerEntry)) == 0

        repeated = await create_or_resume_lava_invoice(db_session, **kwargs)
        assert repeated.id == payment.id
        assert repeated.reconciliation_snapshot == snapshot
        assert calls == ["POST"]

    records = [r for r in caplog.records if r.getMessage() == "lava_top_invoice_creation_failed"]
    assert len(records) == 1
    record = records[0]
    assert record.payment_id == payment.id
    assert record.partner_id == partner.id
    assert record.provider == "lava_top"
    assert record.phase == "create_invoice"
    assert record.error_code == error_code
    assert record.upstream_status == upstream_status
    assert record.elapsed_ms == snapshot["elapsed_ms"]
    assert record.exc_info is None
    serialized = json.dumps(snapshot) + json.dumps(record.__dict__, default=str)
    for private_value in (buyer_email, api_key, private_url, private_body):
        assert private_value not in serialized
