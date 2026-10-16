import asyncio
import json
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from test_native_inference import setup

from app.generations.models import Generation, NativeResponse
from app.infrastructure.config import get_settings
from app.infrastructure.retry import utc_now


async def test_completed_stream_replays_exact_result_without_paid_repeat(client, db_session, monkeypatch):
    calls = []
    events = [
        {"type": "response.output_text.delta", "delta": "answer"},
        {"type": "response.completed", "response": {"id": "private", "usage": {"input_tokens": 2, "output_tokens": 3}}},
    ]
    wire = "".join("data: " + json.dumps(event) + "\n\n" for event in events)

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=wire, headers={"Content-Type": "text/event-stream"})

    partner, headers, upstream = await setup(db_session, monkeypatch, handler)
    body = {"model": "gpt-5.4", "input": "hi", "stream": True, "max_output_tokens": 128}
    first = await client.post("/v1/responses", headers=headers, json=body)
    again = await client.post("/v1/responses", headers=headers, json=body)
    assert first.status_code == again.status_code == 200
    assert first.content == again.content
    assert "answer" in again.text and "private" not in again.text
    assert "text/event-stream" in again.headers["content-type"]
    assert len(calls) == 1
    assert partner.balance_rub == Decimal("999999.99")
    await upstream.aclose()


class SlowBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"output":'
        await asyncio.sleep(0.05)
        yield b"[]}"


async def test_absolute_body_deadline_closes_request_free(client, db_session, monkeypatch):
    partner, headers, upstream = await setup(
        db_session, monkeypatch, lambda request: httpx.Response(200, stream=SlowBody())
    )
    monkeypatch.setattr(get_settings(), "native_request_timeout_seconds", 0.01)
    result = await client.post(
        "/v1/responses", headers=headers, json={"model": "gpt-5.4", "input": "hi", "max_output_tokens": 128}
    )
    assert result.status_code == 503
    generation = await db_session.scalar(select(Generation))
    assert generation.status == "failed" and generation.billing_status == "not_chargeable"
    assert generation.actual_charge_rub is None and partner.balance_rub == Decimal("1000000")
    await upstream.aclose()


async def test_expired_replay_is_tombstone_not_new_submission(client, db_session, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, json={"output": [{"content": "result"}], "usage": {"input_tokens": 2, "output_tokens": 3}}
        )

    partner, headers, upstream = await setup(db_session, monkeypatch, handler)
    body = {"model": "gpt-5.4", "input": "hi", "max_output_tokens": 128}
    first = await client.post("/v1/responses", headers=headers, json=body)
    assert first.status_code == 200
    row = await db_session.scalar(select(NativeResponse))
    row.expires_at = utc_now() - timedelta(seconds=1)
    await db_session.commit()
    again = await client.post("/v1/responses", headers=headers, json=body)
    assert again.status_code == 410
    assert again.json()["error"]["type"] == "idempotent_result_expired"
    assert len(calls) == 1
    assert "result" not in row.encrypted_body
    await upstream.aclose()


async def test_settlement_exception_rolls_back_charge_before_preserving_result(client, db_session, monkeypatch):
    from app.inference.accounting import settle_actual as real_settle

    async def broken_settle(*args, **kwargs):
        await real_settle(*args, **kwargs)
        raise ValueError("injected post-ledger accounting failure")

    partner, headers, upstream = await setup(
        db_session,
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"output": [{"content": "answer"}], "usage": {"input_tokens": 20, "output_tokens": 30}}
        ),
    )
    monkeypatch.setattr("app.inference.router.settle_actual", broken_settle)
    result = await client.post(
        "/v1/responses", headers=headers, json={"model": "gpt-5.4", "input": "hi", "max_output_tokens": 128}
    )
    await db_session.refresh(partner)
    assert result.status_code == 200 and "answer" in result.text
    assert partner.balance_rub == Decimal("1000000")
    generation = await db_session.scalar(select(Generation))
    assert generation.actual_charge_rub is None
    assert generation.billing_status == "pending"
    await upstream.aclose()


async def test_crashed_native_intent_expires_without_submit_replay(client, db_session, monkeypatch):
    from app.accounts.models import ApiKey
    from app.api.dependencies import PartnerAuth
    from app.inference.service import reserve
    from app.providers.models import ProviderAttempt
    from app.workers.generation_worker import process_generation_work_once

    partner, headers, upstream = await setup(
        db_session,
        monkeypatch,
        lambda request: (_ for _ in ()).throw(AssertionError("must never resubmit crashed native job")),
    )
    key = await db_session.scalar(select(ApiKey))
    generation, attempt = await reserve(
        db_session,
        PartnerAuth(partner=partner, api_key=key),
        "responses",
        {"model": "gpt-5.4", "input": "hi", "max_output_tokens": 128},
        "crashed-native-intent",
    )
    generation.created_at = utc_now() - timedelta(hours=1)
    await db_session.commit()
    await process_generation_work_once(db_session)
    await db_session.commit()
    await process_generation_work_once(db_session)
    await db_session.commit()
    assert generation.status == "failed"
    assert generation.billing_status == "not_chargeable" and generation.actual_charge_rub is None
    assert len(list(await db_session.scalars(select(ProviderAttempt)))) == 1
    assert attempt.cost_status == "unknown"
    assert partner.balance_rub == Decimal("1000000")
    await upstream.aclose()


async def test_stored_body_retention_does_not_remove_idempotency_tombstone(client, db_session, monkeypatch):
    from app.workers.generation_worker import process_generation_work_once

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, json={"output": [{"content": "private output"}], "usage": {"input_tokens": 2, "output_tokens": 3}}
        )

    partner, headers, upstream = await setup(db_session, monkeypatch, handler)
    body = {"model": "gpt-5.4", "input": "hi", "max_output_tokens": 128}
    await client.post("/v1/responses", headers=headers, json=body)
    row = await db_session.scalar(select(NativeResponse))
    row.expires_at = utc_now() - timedelta(seconds=1)
    await db_session.commit()
    await process_generation_work_once(db_session)
    await db_session.commit()
    assert not list(await db_session.scalars(select(NativeResponse)))
    assert len(list(await db_session.scalars(select(Generation)))) == 1
    again = await client.post("/v1/responses", headers=headers, json=body)
    assert again.status_code == 410 and len(calls) == 1
    await upstream.aclose()


async def test_unresolved_delivered_usage_is_waived_after_deadline(client, db_session, monkeypatch):
    from fastapi import HTTPException

    from app.inference.accounting import settle_actual
    from app.workers.generation_worker import process_generation_work_once

    partner, headers, upstream = await setup(
        db_session, monkeypatch, lambda request: httpx.Response(200, json={"output": [{"content": "valid result"}]})
    )
    response = await client.post(
        "/v1/responses", headers=headers, json={"model": "gpt-5.4", "input": "hi", "max_output_tokens": 128}
    )
    assert response.status_code == 200
    generation = await db_session.scalar(select(Generation))
    generation.client_finalized_at = utc_now() - timedelta(hours=1)
    await db_session.commit()
    await process_generation_work_once(db_session)
    await db_session.commit()
    assert generation.status == "completed" and generation.billing_status == "waived"
    with pytest.raises(HTTPException) as exc:
        await settle_actual(db_session, generation, {"input_tokens": 100, "output_tokens": 100})
    assert exc.value.status_code == 409
    assert partner.balance_rub == Decimal("1000000")
    await upstream.aclose()
