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
    headers["X-Submission-Receipt"] = "v1"
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
    for model in (ProviderAttempt, LedgerEntry):
        assert (await db_session.execute(select(model))).scalars().all() == []
    rejected = (await db_session.execute(select(Generation))).scalar_one()
    assert rejected.status == "failed" and rejected.billing_status == "not_chargeable"
    assert rejected.partner_price_rub == rejected.provider_cost_hold_usdt == 0
    assert rejected.actual_charge_rub == 0
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
    headers["X-Submission-Receipt"] = "v1"
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
    headers["X-Submission-Receipt"] = "v1"
    response = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "gpt-5.4", "messages": [{"role": "user", "content": "test"}]},
    )
    assert response.status_code == 503
    assert calls and "X-Submission-State" not in response.headers
    assert (await db_session.execute(select(Generation))).scalars().all()
    await upstream.aclose()


async def test_rollback_failure_cannot_emit_no_acceptance_receipt(client, db_session, monkeypatch, caplog):
    from app.inference.service import InferenceAdmissionError

    _, headers, upstream = await setup(db_session, monkeypatch, lambda request: httpx.Response(503))
    headers["X-Client-Request-Id"] = RID
    headers["X-Submission-Receipt"] = "v1"
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
    events = [r for r in caplog.records if r.message == "native_inference_rejected"]
    assert [(r.failure_stage, r.http_status) for r in events] == [("admission_fence", 500)]
    await upstream.aclose()


async def test_receipted_key_cannot_be_accepted_after_capacity_recovers(client, db_session, monkeypatch):
    _, headers, upstream = await setup(
        db_session,
        monkeypatch,
        lambda request: httpx.Response(202),
        model="seedance-2.5",
        category="video",
        rates=[("default", "480p", "second", Decimal("20"), Decimal(".078"))],
    )
    headers.update({"X-Client-Request-Id": RID, "X-Submission-Receipt": "v1"})
    body = {"model": "seedance-2.5", "prompt": "test", "duration": 4, "resolution": "480p"}
    balance = AsyncMock(return_value=Decimal(0))
    monkeypatch.setattr(ArgoLinkAdapter, "prepaid_balance_usdt", balance)
    first = await client.post("/v1/videos/generations", headers=headers, json=body)
    assert first.status_code == 503 and first.headers.get("X-Submission-State") == "not-accepted-v1"
    balance.return_value = Decimal("1000000")
    again = await client.post("/v1/videos/generations", headers=headers, json=body)
    assert again.status_code == 503 and again.headers.get("X-Submission-State") == "not-accepted-v1"
    assert (await db_session.execute(select(Generation))).scalar_one().status == "failed"
    assert (await db_session.execute(select(ProviderAttempt))).scalars().all() == []
    fresh_headers = {
        **headers,
        "Idempotency-Key": "fresh-independent-request",
        "X-Client-Request-Id": "fresh-request-correlation",
    }
    fresh = await client.post("/v1/videos/generations", headers=fresh_headers, json=body)
    assert fresh.status_code == 202
    await upstream.aclose()


async def test_legacy_correlated_requests_retain_retryable_rejections_without_receipt(client, db_session, monkeypatch):
    _, headers, upstream = await setup(
        db_session,
        monkeypatch,
        lambda request: httpx.Response(202),
        model="seedance-2.5",
        category="video",
        rates=[("default", "480p", "second", Decimal("20"), Decimal(".078"))],
    )
    headers["X-Client-Request-Id"] = RID
    body = {"model": "seedance-2.5", "prompt": "test", "duration": 4, "resolution": "480p"}
    balance = AsyncMock(return_value=Decimal(0))
    monkeypatch.setattr(ArgoLinkAdapter, "prepaid_balance_usdt", balance)
    rejected = await client.post("/v1/videos/generations", headers=headers, json=body)
    assert rejected.status_code == 503 and "X-Submission-State" not in rejected.headers
    assert (await db_session.execute(select(Generation))).scalars().all() == []
    balance.return_value = Decimal("1000000")
    accepted = await client.post("/v1/videos/generations", headers=headers, json=body)
    assert accepted.status_code == 202
    await upstream.aclose()


@pytest.mark.parametrize("stage", ["provider_credential", "pricing", "provider_capital", "provider_circuit"])
async def test_receipt_does_not_identify_procurement_failure_stage(client, db_session, monkeypatch, stage):
    from app.inference.service import InferenceAdmissionError

    _, headers, upstream = await setup(db_session, monkeypatch, lambda request: httpx.Response(503))
    headers.update({"X-Client-Request-Id": RID, "X-Submission-Receipt": "v1"})
    monkeypatch.setattr(
        "app.inference.router.reserve",
        AsyncMock(side_effect=InferenceAdmissionError(503, "provider_temporarily_unavailable", failure_stage=stage)),
    )
    response = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "gpt-5.4", "messages": [{"role": "user", "content": "test"}]},
    )
    assert response.status_code == 503
    assert response.headers.get("X-Submission-State") == "not-accepted-v1"
    assert stage not in response.text and stage not in str(response.headers)
    await upstream.aclose()
