from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from test_native_inference import setup

from app.accounts.models import ApiKey
from app.api.dependencies import PartnerAuth
from app.generations.models import Generation
from app.inference.accounting import token_usage
from app.inference.recovery import recover_native_outcomes_once
from app.inference.service import reserve
from app.infrastructure.retry import utc_now


@pytest.mark.parametrize("details", ["malformed", [1], 1])
def test_malformed_nested_usage_is_a_validation_error(details):
    with pytest.raises(ValueError, match="invalid_usage"):
        token_usage("responses", {"input_tokens": 2, "output_tokens": 3, "input_tokens_details": details})


async def test_valid_result_with_malformed_nested_usage_is_recoverable(client, db_session, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={
            "output": [{"content": "preserved answer"}],
            "usage": {"input_tokens": 2, "output_tokens": 3, "input_tokens_details": "malformed"},
        })

    partner, headers, upstream = await setup(db_session, monkeypatch, handler)
    body = {"model": "gpt-5.4", "input": "hi", "max_output_tokens": 128}
    first = await client.post("/v1/responses", headers=headers, json=body)
    again = await client.post("/v1/responses", headers=headers, json=body)
    assert first.status_code == again.status_code == 200
    assert first.content == again.content and "preserved answer" in first.text
    assert len(calls) == 1
    generation = await db_session.scalar(select(Generation))
    assert generation.billing_status == "pending" and generation.actual_charge_rub is None
    assert partner.balance_rub == Decimal("1000000")
    await upstream.aclose()


async def test_unexpired_usage_review_cannot_starve_expired_request(db_session, monkeypatch):
    def handler(request):
        raise AssertionError("Recovery must not submit provider work")

    partner, _, upstream = await setup(db_session, monkeypatch, handler)
    key = await db_session.scalar(select(ApiKey))
    auth = PartnerAuth(partner=partner, api_key=key)
    body = {"model": "gpt-5.4", "input": "hi", "max_output_tokens": 128}
    waiting, _ = await reserve(db_session, auth, "responses", body, "waiting-usage")
    waiting.created_at = utc_now() - timedelta(hours=2)
    waiting.status = "completed"
    waiting.client_finalized_at = utc_now()
    waiting.request_payload = {**waiting.request_payload, "usage_reconciliation_pending": True}
    expired, _ = await reserve(db_session, auth, "responses", body, "expired-request")
    expired.created_at = utc_now() - timedelta(hours=1)
    await db_session.commit()
    count = await recover_native_outcomes_once(db_session, limit=1)
    await db_session.commit()
    assert count == 1
    assert expired.status == "failed" and expired.billing_status == "not_chargeable"
    assert waiting.status == "completed" and waiting.billing_status == "pending"
    await upstream.aclose()


@pytest.mark.integration
async def test_postgres_recovery_filters_due_work_before_batch_limit():
    from test_native_postgres_finality import cleanup, infrastructure, seed

    engine, factory = infrastructure()
    first = await seed(factory)
    second = await seed(factory)
    try:
        async with factory() as db:
            waiting = await db.get(Generation, first[1])
            expired = await db.get(Generation, second[1])
            waiting.created_at = utc_now() - timedelta(hours=2)
            waiting.status = "completed"
            waiting.client_finalized_at = utc_now()
            waiting.request_payload = {
                **waiting.request_payload,
                "native_finality_policy": "v1",
                "usage_reconciliation_pending": True,
            }
            expired.created_at = utc_now() - timedelta(hours=1)
            expired.request_payload = {**expired.request_payload, "native_finality_policy": "v1"}
            await db.commit()
        async with factory() as db:
            assert await recover_native_outcomes_once(db, limit=1) == 1
            await db.commit()
        async with factory() as db:
            assert (await db.get(Generation, first[1])).billing_status == "pending"
            assert (await db.get(Generation, second[1])).billing_status == "not_chargeable"
    finally:
        await cleanup(factory, first)
        await cleanup(factory, second)
        await engine.dispose()
