"""Customer-visible finality must not depend on procurement reconciliation."""

from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from test_native_inference import setup

from app.billing.models import LedgerEntry
from app.generations.models import Generation
from app.inference.accounting import settle_actual
from app.inference.router import fail, finish
from app.providers.models import ProviderAttempt
from app.webhooks.models import WebhookEvent


@pytest.mark.parametrize("status", [408, 500, 502, 503, 504])
async def test_failed_sync_response_is_terminal_without_partner_charge(client, db_session, monkeypatch, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": "private upstream error"}})

    partner, headers, upstream = await setup(db_session, monkeypatch, handler)
    from app.accounts.models import ApiKey

    key = await db_session.scalar(select(ApiKey))
    key.webhook_url = "https://partner.example/hooks/generations"
    await db_session.commit()
    result = await client.post(
        "/v1/responses", headers=headers, json={"model": "gpt-5.4", "input": "hi", "max_output_tokens": 128}
    )
    assert result.status_code == 503
    generation = await db_session.scalar(select(Generation))
    attempt = await db_session.scalar(select(ProviderAttempt))
    assert generation.status == "failed"
    assert generation.actual_charge_rub is None
    assert attempt.cost_status == "unknown"
    assert attempt.status == "reconciliation_required"
    assert generation.provider_cost_hold_usdt > 0
    assert partner.balance_rub == Decimal("1000000")
    events = list(await db_session.scalars(select(WebhookEvent)))
    assert len(events) == 1 and events[0].event_type == "failed"
    assert len(calls) == 1
    assert "private upstream error" not in result.text
    assert "unknown" not in result.text
    await upstream.aclose()


async def closed_request(client, db_session, monkeypatch):
    partner, headers, upstream = await setup(
        db_session, monkeypatch, lambda request: httpx.Response(502, json={"error": {"message": "failed"}})
    )
    await client.post(
        "/v1/responses", headers=headers, json={"model": "gpt-5.4", "input": "hi", "max_output_tokens": 128}
    )
    return (
        partner,
        await db_session.scalar(select(Generation)),
        await db_session.scalar(select(ProviderAttempt)),
        upstream,
    )


async def test_late_finish_cannot_charge_or_resurrect_closed_request(client, db_session, monkeypatch):
    partner, generation, attempt, upstream = await closed_request(client, db_session, monkeypatch)
    await finish(db_session, generation, attempt, {"input_tokens": 20, "output_tokens": 30})
    await finish(db_session, generation, attempt, {"input_tokens": 20, "output_tokens": 30})
    await db_session.refresh(partner)
    assert generation.status == "failed"
    assert generation.actual_charge_rub is None
    assert partner.balance_rub == Decimal("1000000")
    assert not list(await db_session.scalars(select(LedgerEntry)))
    await upstream.aclose()


async def test_direct_usage_settlement_rejects_closed_request(client, db_session, monkeypatch):
    partner, generation, attempt, upstream = await closed_request(client, db_session, monkeypatch)
    with pytest.raises(HTTPException) as error:
        await settle_actual(db_session, generation, {"input_tokens": 20, "output_tokens": 30})
    assert error.value.status_code == 409
    assert partner.balance_rub == Decimal("1000000")
    await upstream.aclose()


async def test_repeated_failure_creates_one_terminal_event(client, db_session, monkeypatch):
    partner, generation, attempt, upstream = await closed_request(client, db_session, monkeypatch)
    generation.webhook_url_snapshot = "https://partner.example/hooks"
    await db_session.commit()
    await fail(db_session, generation, attempt, "provider_temporarily_unavailable", definitive=False)
    await fail(db_session, generation, attempt, "provider_temporarily_unavailable", definitive=False)
    assert generation.status == "failed"
    events = list(await db_session.scalars(select(WebhookEvent)))
    assert len(events) == 1
    await upstream.aclose()
