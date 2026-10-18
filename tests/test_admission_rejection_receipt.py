"""A rejected admission is safe to fail over only with a correlated receipt."""

import hashlib
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from test_native_inference import setup

from app.billing.models import LedgerEntry
from app.generations.models import Generation
from app.providers.argolink import ArgoLinkAdapter
from app.providers.models import ProviderAttempt

RID = "d3252064-e54b-4590-a56f-ae162ef7b9ec"


@pytest.mark.parametrize("video", [True, False])
async def test_capital_rejection_is_correlated_and_has_no_accepted_work(client, db_session, monkeypatch, video):
    calls = []

    def upstream_handler(request):
        calls.append(request)
        raise AssertionError("Capital rejection must not submit paid work")

    args = (
        dict(
            model="seedance-2.5",
            category="video",
            rates=[
                ("default", "480p", "second", Decimal("20"), Decimal(".078")),
            ],
        )
        if video
        else dict(model="qwen3.8-max")
    )
    partner, headers, upstream = await setup(db_session, monkeypatch, upstream_handler, **args)
    partner_id = partner.id
    headers["X-Client-Request-Id"] = RID
    body = (
        {"model": "seedance-2.5", "prompt": "test", "duration": 4, "resolution": "480p"}
        if video
        else {
            "model": "qwen3.8-max",
            "messages": [{"role": "user", "content": "test"}],
            "stream": False,
        }
    )
    monkeypatch.setattr(ArgoLinkAdapter, "prepaid_balance_usdt", AsyncMock(return_value=Decimal(0)))
    response = await client.post(
        "/v1/" + ("videos/generations" if video else "chat/completions"), headers=headers, json=body
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "provider_temporarily_unavailable"}
    assert response.headers.get("X-Submission-State") == "not-accepted-v1"
    assert response.headers.get("X-Client-Request-Id") == RID
    assert (
        response.headers.get("X-Idempotency-Key-Sha256")
        == hashlib.sha256(headers["Idempotency-Key"].encode()).hexdigest()
    )
    assert calls == []
    for model in (Generation, ProviderAttempt, LedgerEntry):
        assert (await db_session.execute(select(model))).scalars().all() == []
    await db_session.refresh(partner)
    assert partner.id == partner_id and partner.balance_rub == Decimal("1000000")
    await upstream.aclose()


async def test_accepted_idempotent_video_never_gets_rejection_receipt(client, db_session, monkeypatch):
    partner, headers, upstream = await setup(
        db_session,
        monkeypatch,
        lambda request: httpx.Response(202),
        model="seedance-2.5",
        category="video",
        rates=[("default", "480p", "second", Decimal("20"), Decimal(".078"))],
    )
    headers["X-Client-Request-Id"] = RID
    body = {"model": "seedance-2.5", "prompt": "test", "duration": 4, "resolution": "480p"}
    first = await client.post("/v1/videos/generations", headers=headers, json=body)
    assert first.status_code == 202
    monkeypatch.setattr(ArgoLinkAdapter, "prepaid_balance_usdt", AsyncMock(return_value=Decimal(0)))
    duplicate = await client.post("/v1/videos/generations", headers=headers, json=body)
    assert duplicate.status_code == 202 and duplicate.json() == first.json()
    assert "X-Submission-State" not in duplicate.headers
    await upstream.aclose()


async def test_generic_503_does_not_authorize_failover(client, db_session, monkeypatch):
    _, headers, upstream = await setup(db_session, monkeypatch, lambda request: httpx.Response(503))
    monkeypatch.setattr(
        "app.inference.router.reserve", AsyncMock(side_effect=HTTPException(503, "provider_temporarily_unavailable"))
    )
    response = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "gpt-5.4", "messages": [{"role": "user", "content": "test"}]},
    )
    assert response.status_code == 503
    assert "X-Submission-State" not in response.headers
    await upstream.aclose()


async def test_post_acceptance_503_has_no_admission_receipt(client, db_session, monkeypatch):
    calls = []

    def failed_provider(request):
        calls.append(request)
        return httpx.Response(503, json={"error": "unavailable"})

    _, headers, upstream = await setup(db_session, monkeypatch, failed_provider)
    headers["X-Client-Request-Id"] = RID
    response = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "gpt-5.4", "messages": [{"role": "user", "content": "test"}]},
    )
    assert response.status_code == 503
    assert calls and "X-Submission-State" not in response.headers
    assert (await db_session.execute(select(Generation))).scalars().all()
    await upstream.aclose()


async def test_rollback_failure_cannot_emit_no_acceptance_receipt(client, db_session, monkeypatch):
    from app.inference.service import InferenceAdmissionError

    _, headers, upstream = await setup(db_session, monkeypatch, lambda request: httpx.Response(503))
    headers["X-Client-Request-Id"] = RID
    monkeypatch.setattr(
        "app.inference.router.reserve",
        AsyncMock(
            side_effect=InferenceAdmissionError(
                503, "provider_temporarily_unavailable", failure_stage="provider_capital"
            )
        ),
    )
    monkeypatch.setattr(db_session, "rollback", AsyncMock(side_effect=RuntimeError("rollback unavailable")))
    with pytest.raises(RuntimeError, match="rollback unavailable"):
        await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"model": "gpt-5.4", "messages": [{"role": "user", "content": "test"}]},
        )
    await upstream.aclose()
