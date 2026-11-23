"""Replay restores the committed result, never submits another paid request."""

import base64
import io
from decimal import Decimal

import httpx
import pytest
from PIL import Image
from sqlalchemy import select
from test_native_inference import setup

from app.billing.models import LedgerEntry
from app.generations.models import Generation
from app.infrastructure.config import get_settings


async def test_completed_json_replays_without_second_charge(client, db_session, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "id": "private",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "committed result"}]}],
                "usage": {"input_tokens": 20, "output_tokens": 30},
            },
        )

    partner, headers, upstream = await setup(db_session, monkeypatch, handler)
    body = {"model": "gpt-5.4", "input": "hi", "max_output_tokens": 128}
    first = await client.post("/v1/responses", headers=headers, json=body)
    replay = await client.post("/v1/responses", headers=headers, json=body)
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert first.headers["x-request-id"] == replay.headers["x-request-id"]
    assert len(calls) == 1
    assert len(list(await db_session.scalars(select(LedgerEntry)))) == 1
    assert partner.balance_rub == Decimal("999999.95")
    changed = await client.post("/v1/responses", headers=headers, json={**body, "input": "different"})
    assert changed.status_code == 409 and len(calls) == 1
    await upstream.aclose()


async def test_final_error_replays_without_second_provider_submission(client, db_session, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "7"}, json={"code": "rate_limit_exceeded"})

    partner, headers, upstream = await setup(db_session, monkeypatch, handler)
    body = {"model": "gpt-5.4", "input": "hi", "max_output_tokens": 128}
    first = await client.post("/v1/responses", headers=headers, json=body)
    replay = await client.post("/v1/responses", headers=headers, json=body)
    assert first.status_code == replay.status_code == 429
    assert first.json() == replay.json()
    assert replay.headers["retry-after"] == "7"
    assert len(calls) == 1 and partner.balance_rub == Decimal("1000000")
    await upstream.aclose()



@pytest.mark.parametrize(
    ("model", "encoding", "size"),
    [
        ("seedream-5.0-pro", "JPEG", (1600, 600)),
        ("seedream-5.5", "PNG", (350, 2100)),
        ("seedream-5.0-pro", "PNG", (1600, 600)),
    ],
)
async def test_off_contract_but_verified_image_reaches_partner_and_idempotent_replay(
    client, db_session, monkeypatch, model, encoding, size
):
    # The supplier returned real bytes. Ratio/size/encoding differences are
    # not grounds for losing a completed job; nor for charging exact-size usage.
    from app.billing.models import LedgerEntry
    from app.providers.models import ProviderAttempt

    buf = io.BytesIO()
    Image.new("RGB", size, color="white").save(buf, format=encoding)
    result = {"data": [{"b64_json": base64.b64encode(buf.getvalue()).decode()}]}
    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(200, json=result)

    partner, headers, upstream = await setup(
        db_session, monkeypatch, provider, model=model, category="image",
        rates=[("default", "1K", "generation", Decimal("10"), Decimal(".03"))],
    )
    body = {"model": model, "prompt": "Original", "resolution": "1k", "aspect_ratio": "3:4"}
    try:
        response = await client.post("/v1/images/generations", headers=headers, json=body)
        assert response.status_code == 200, response.text[:400]
        assert response.json()["data"][0]["b64_json"] == result["data"][0]["b64_json"]
        generated = await db_session.scalar(select(Generation))
        attempt = await db_session.scalar(select(ProviderAttempt))
        assert generated.status == "completed"
        assert generated.actual_charge_rub is None
        assert generated.request_payload["usage_reconciliation_pending"] is True
        assert attempt.cost_status == "unknown"
        assert partner.balance_rub == Decimal("1000000")
        assert not list(await db_session.scalars(select(LedgerEntry)))
        replay = await client.post("/v1/images/generations", headers=headers, json=body)
        assert replay.status_code == 200 and replay.content == response.content
        assert len(calls) == 1
        assert partner.balance_rub == Decimal("1000000")
    finally:
        await upstream.aclose()

async def test_valid_image_survives_usage_failure(client, db_session, monkeypatch, tmp_path):
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64)).save(buffer, format="PNG")
    output = {"data": [{"b64_json": base64.b64encode(buffer.getvalue()).decode()}]}
    partner, headers, upstream = await setup(
        db_session,
        monkeypatch,
        lambda request: httpx.Response(200, json=output),
        model="nano-banana-2.1",
        category="image",
        rates=[("default", tier, "generation", Decimal("10"), Decimal(".03")) for tier in ("1K", "2K", "4K")],
    )
    monkeypatch.setattr(get_settings(), "media_local_storage_dir", str(tmp_path))

    def corrupt_usage(*args):
        raise ValueError("malformed usage, not malformed image")

    monkeypatch.setattr("app.inference.router.image_usage", corrupt_usage)
    body = {"model": "nano-banana-2.1", "prompt": "test", "resolution": "1K", "n": 1}
    first = await client.post("/v1/images/generations", headers=headers, json=body)
    assert first.status_code == 200
    assert first.json() == output
    generation = await db_session.scalar(select(Generation))
    assert generation.status == "completed" and generation.actual_charge_rub is None
    assert generation.result_url
    assert generation.billing_status == "pending"
    assert partner.balance_rub == Decimal("1000000")
    replay = await client.post("/v1/images/generations", headers=headers, json=body)
    assert replay.status_code == 200 and replay.json() == output
    await upstream.aclose()
