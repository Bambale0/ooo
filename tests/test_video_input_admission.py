import copy
import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_native_inference import setup
from test_video_input_staging import BODY, COPY, DATA, install_transport

from app.billing.models import CoverageLedgerEntry, LedgerEntry
from app.generations.models import Generation
from app.generations.service import _provider_request_for_generation, dispatch_generation_to_provider
from app.media import video_inputs
from app.providers.argolink import ArgoLinkAdapter
from app.providers.models import ProviderAttempt


@pytest.mark.parametrize("edit, expected_seconds", [(True, 10), (False, 9)])
async def test_measured_video_admission_preserves_original_identity_and_staged_bytes(
    client, db_session, monkeypatch, edit, expected_seconds
):
    partner, headers, upstream = await setup(
        db_session, monkeypatch, lambda request: pytest.fail("No paid provider request during admission"),
        model="seedance-2.5", category="video",
        rates=[("default", "480p", "second", Decimal("20"), Decimal(".078"))],
    )
    body = {"model": "seedance-2.5", "prompt": "Animate", "resolution": "480p",
            "reference_videos": [{"url": "https://source.example/clip.mp4"}]}
    body.update({"omni_reference_task_type": "edit"} if edit else {"duration": 4})
    staged = copy.deepcopy(body)
    staged["reference_videos"][0]["url"] = "https://media.example/private-copy.mp4"
    preparation = {"policy": "isolated-full-decode-v1", "body": staged, "billable_seconds": expected_seconds,
                   "output_seconds": None if edit else 4, "expires_at": int(time.time()) + 604800,
                   "assets": [{"sha256": "a" * 64, "size_bytes": 100, "duration": "5/1"}]}
    prepare = AsyncMock(return_value=preparation)
    monkeypatch.setattr("app.inference.service.prepare_video_inputs", prepare, raising=False)
    response = await client.post("/v1/videos/generations", json=body, headers=headers)
    assert response.status_code == 202, response.text
    generation = await db_session.get(Generation, response.json()["request_id"])
    assert generation.partner_price_rub == Decimal(expected_seconds * 20)
    assert generation.provider_cost_usdt_snapshot == Decimal(".078") * expected_seconds
    assert generation.request_payload["native_body"] == body
    assert generation.request_payload["effective_native_body"] == staged
    assert generation.request_payload["reserved_units"] == {"seconds": expected_seconds}
    request = _provider_request_for_generation(generation)
    assert request.native_body == staged
    assert request.reference_videos == ("https://media.example/private-copy.mp4",)
    duplicate = await client.post("/v1/videos/generations", json=body, headers=headers)
    assert duplicate.json() == response.json()
    assert prepare.await_count == 1
    assert len(list(await db_session.scalars(select(LedgerEntry)))) == 1
    assert len(list(await db_session.scalars(select(CoverageLedgerEntry)))) == 1
    assert partner.balance_rub == Decimal("1000000") - expected_seconds * 20
    await upstream.aclose()


async def measured_admission(client, db_session, monkeypatch):
    submitted = []

    def handler(request):
        import json

        submitted.append(json.loads(request.content))
        return httpx.Response(202, json={"request_id": "mock-measured-job"})

    partner, headers, upstream = await setup(
        db_session, monkeypatch, handler, model="seedance-2.5", category="video",
        rates=[("default", "720p", "second", Decimal("20"), Decimal(".078"))],
    )
    calls, uploads, tickets = install_transport(monkeypatch)
    monkeypatch.setattr("app.inference.service.prepare_video_inputs", video_inputs.prepare_video_inputs)
    response = await client.post("/v1/videos/generations", json=BODY, headers=headers)
    assert response.status_code == 202, response.text
    generation = await db_session.get(Generation, response.json()["request_id"])
    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", AsyncMock(
        return_value=ArgoLinkAdapter(api_key="fixture", client=upstream),
    ))
    monkeypatch.setattr("app.generations.service.get_provider_rate_limiter",
                        lambda *_: SimpleNamespace(acquire=AsyncMock()))
    return partner, generation, upstream, submitted, (calls, uploads, tickets)


async def test_real_inspection_copy_admission_and_restart_dispatch_share_identical_media(
    client, db_session, monkeypatch,
):
    partner, generation, upstream, submitted, (calls, uploads, tickets) = await measured_admission(
        client, db_session, monkeypatch,
    )
    assert generation.partner_price_rub == 200
    assert uploads == [DATA] and len(tickets) == 1 and len(calls) == 3
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    async with factory() as restarted:
        durable = await restarted.get(Generation, generation.id)
        attempt = await dispatch_generation_to_provider(restarted, durable)
        assert attempt.provider_task_id == "mock-measured-job"
        assert submitted == [{**BODY, "reference_videos": [{"url": COPY}]}]
        await dispatch_generation_to_provider(restarted, durable)
        assert len(submitted) == 1
    assert partner.balance_rub == Decimal("999800")
    await upstream.aclose()


@pytest.mark.parametrize("after_pacing", [False, True])
async def test_expired_first_submit_releases_existing_reserves_once_without_paid_call(
    client, db_session, monkeypatch, after_pacing,
):
    partner, generation, upstream, submitted, _ = await measured_admission(client, db_session, monkeypatch)
    generation.webhook_url_snapshot = "https://client.example/hook"
    if after_pacing:
        async def delayed():
            monkeypatch.setattr(video_inputs, "snapshot_is_fresh", lambda *_: False)

        monkeypatch.setattr("app.generations.service.get_provider_rate_limiter",
                            lambda *_: SimpleNamespace(acquire=delayed))
    else:
        monkeypatch.setattr(video_inputs, "snapshot_is_fresh", lambda *_: False)
    attempt = await dispatch_generation_to_provider(db_session, generation)
    assert not submitted
    assert generation.status == attempt.status == "cancelled"
    assert generation.public_error_code == "input_media_expired"
    assert partner.balance_rub == partner.cost_coverage_rub == Decimal("1000000")
    await dispatch_generation_to_provider(db_session, generation)
    entries = list(await db_session.scalars(select(LedgerEntry)))
    assert [item.operation_type for item in entries].count("generation_reserve_release") == 1
    from app.webhooks.models import WebhookEvent

    events = list(await db_session.scalars(select(WebhookEvent)))
    assert len(events) == 1 and events[0].payload["error_code"] == "input_media_expired"
    await upstream.aclose()


@pytest.mark.parametrize("status", ["retry_pending", "reconciliation_required", "submitting"])
async def test_expiry_preserves_prior_unknown_obligation_without_replay_or_credit(
    client, db_session, monkeypatch, status,
):
    partner, generation, upstream, submitted, _ = await measured_admission(client, db_session, monkeypatch)
    attempt = ProviderAttempt(generation_id=generation.id, provider="argolink", status=status)
    db_session.add(attempt)
    await db_session.commit()
    before = (partner.balance_rub, partner.cost_coverage_rub)
    monkeypatch.setattr(video_inputs, "snapshot_is_fresh", lambda *_: False)
    assert await dispatch_generation_to_provider(db_session, generation) is attempt
    assert not submitted and not attempt.provider_task_id
    assert (partner.balance_rub, partner.cost_coverage_rub) == before
    assert len(list(await db_session.scalars(select(LedgerEntry)))) == 1
    assert len(list(await db_session.scalars(select(CoverageLedgerEntry)))) == 1
    await upstream.aclose()
