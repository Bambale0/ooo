"""No partner prepayment holds: charge only confirmed usage and release legacy holds once."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
from sqlalchemy import select
from test_native_inference import setup

from app.billing.models import LedgerEntry
from app.billing.service import apply_partner_balance_change
from app.generations.models import Generation
from app.generations.service import dispatch_generation_to_provider, poll_generation_provider
from app.inference.accounting import settle_actual
from app.infrastructure.retry import utc_now
from app.providers.argolink import ArgoLinkAdapter


async def _entries(db, generation):
    return list(
        await db.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.generation_id == generation.id)
            .order_by(LedgerEntry.created_at, LedgerEntry.id)
        )
    )


async def test_qwen_actual_only_charge_does_not_require_huge_quoted_balance(client, db_session, monkeypatch):
    def upstream(request):
        return httpx.Response(
            200,
            json={
                "id": "test-qwen-provider-id",
                "choices": [{"message": {"role": "assistant", "content": "Done"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            },
        )

    partner, headers, provider = await setup(db_session, monkeypatch, upstream, model="qwen3.8-max")
    partner.balance_rub = Decimal("0.50")
    await db_session.commit()
    try:
        response = await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"model": "qwen3.8-max", "messages": [{"role": "user", "content": "Say Done"}]},
        )
        assert response.status_code == 200, response.text
        generation = await db_session.get(Generation, response.json()["id"])
        assert Decimal("0.50") < generation.partner_price_rub < Decimal("100")
        assert generation.actual_charge_rub == Decimal("0.03")
        await db_session.refresh(partner)
        assert partner.balance_rub == Decimal("0.47")
        assert [(e.operation_type, e.amount_rub) for e in await _entries(db_session, generation)] == [
            ("generation_usage_charge", Decimal("-0.03"))
        ]
    finally:
        await provider.aclose()


async def test_real_usage_exceeding_remaining_balance_creates_debt_and_blocks_next_request(
    client, db_session, monkeypatch
):
    requests = []

    def provider_reply(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "cost-exceeds-small-wallet",
                "choices": [{"message": {"content": "OK"}}],
                "usage": {"prompt_tokens": 70, "completion_tokens": 30, "total_tokens": 100},
            },
        )

    partner, headers, upstream = await setup(db_session, monkeypatch, provider_reply, model="qwen3.8-max")
    partner.balance_rub = Decimal("0.01")
    await db_session.commit()
    try:
        body = {"model": "qwen3.8-max", "messages": [{"role": "user", "content": "Hello"}]}
        response = await client.post("/v1/chat/completions", headers=headers, json=body)
        assert response.status_code == 200, response.text
        await db_session.refresh(partner)
        assert partner.balance_rub == Decimal("-0.09")
        generation = await db_session.get(Generation, response.json()["id"])
        assert generation.actual_charge_rub == Decimal("0.10")
        assert len(requests) == 1
        rejected = await client.post(
            "/v1/chat/completions",
            headers={**headers, "Idempotency-Key": "different-qwen-task"},
            json=body,
        )
        assert rejected.status_code == 402
        assert len(requests) == 1
        assert len(list(await db_session.scalars(select(Generation)))) == 1
    finally:
        await upstream.aclose()


async def test_qwen_502_keeps_no_partner_hold_or_charge(client, db_session, monkeypatch):
    partner, headers, provider = await setup(
        db_session,
        monkeypatch,
        lambda request: httpx.Response(502, json={"error": {"message": "upstream transient"}}),
        model="qwen3.8-max",
    )
    partner.balance_rub = Decimal("0.50")
    await db_session.commit()
    try:
        body = {"model": "qwen3.8-max", "messages": [{"role": "user", "content": "Hello"}]}
        result = await client.post("/v1/chat/completions", headers=headers, json=body)
        assert result.status_code == 503, result.text
        generation = (await db_session.scalars(select(Generation))).one()
        assert generation.status == "failed" and generation.billing_status == "not_chargeable"
        assert generation.actual_charge_rub is None
        await db_session.refresh(partner)
        assert partner.balance_rub == Decimal("0.50")
        assert await _entries(db_session, generation) == []
        second = await client.post("/v1/chat/completions", headers=headers, json=body)
        assert second.status_code == 503 and second.json() == result.json()
    finally:
        await provider.aclose()


async def test_video_charges_after_verified_seconds_and_never_advances_quote(client, db_session, monkeypatch):
    partner, headers, provider = await setup(
        db_session,
        monkeypatch,
        lambda request: httpx.Response(500),
        model="seedance-2.5",
        category="video",
        rates=[("default", "480p", "second", Decimal("20"), Decimal("0.078"))],
    )
    partner.balance_rub = Decimal("150.00")
    await db_session.commit()
    body = {
        "model": "seedance-2.5",
        "prompt": "Change the colors of this reference video",
        "resolution": "480p",
        "duration": 5,
        "reference_videos": [{"url": "https://media.example.org/clip.mp4"}],
    }
    try:
        response = await client.post("/v1/videos/generations", headers=headers, json=body)
        assert response.status_code == 202, response.text
        generation = await db_session.get(Generation, response.json()["request_id"])
        assert generation.partner_price_rub > Decimal("150.00")
        await db_session.refresh(partner)
        assert partner.balance_rub == Decimal("150.00")
        assert await _entries(db_session, generation) == []
        await settle_actual(db_session, generation, {"seconds": 5})
        await settle_actual(db_session, generation, {"seconds": 5})
        await db_session.commit()
        await db_session.refresh(partner)
        assert generation.actual_charge_rub == Decimal("100.00")
        assert partner.balance_rub == Decimal("50.00")
        assert [(e.operation_type, e.amount_rub) for e in await _entries(db_session, generation)] == [
            ("generation_usage_charge", Decimal("-100.00"))
        ]
    finally:
        await provider.aclose()


async def test_older_legacy_hold_returns_once_and_late_success_only_charges_actual(client, db_session, monkeypatch):
    partner, headers, provider = await setup(
        db_session,
        monkeypatch,
        lambda request: httpx.Response(500),
        model="seedance-2.5",
        category="video",
        rates=[("default", "480p", "second", Decimal("20"), Decimal("0.078"))],
    )
    partner.balance_rub = Decimal("200.00")
    await db_session.commit()
    from app.workers.generation_worker import release_stale_partner_reserves_once

    try:
        response = await client.post(
            "/v1/videos/generations",
            headers=headers,
            json={"model": "seedance-2.5", "prompt": "Gentle slow motion", "duration": 4, "resolution": "480p"},
        )
        assert response.status_code == 202, response.text
        generation = await db_session.get(Generation, response.json()["request_id"])
        generation.status = "reconciliation_required"
        generation.created_at = utc_now() - timedelta(minutes=31)
        await apply_partner_balance_change(
            db_session,
            partner,
            Decimal("-80.00"),
            "generation_reserve",
            f"generation-reserve:{generation.id}",
            generation.id,
            "Historical partner hold",
            allow_negative=False,
        )
        await db_session.commit()
        assert partner.balance_rub == Decimal("120.00")
        assert await release_stale_partner_reserves_once(db_session) == 1
        await db_session.commit()
        assert await release_stale_partner_reserves_once(db_session) == 0
        await db_session.commit()
        await db_session.refresh(partner)
        assert partner.balance_rub == Decimal("200.00")
        await settle_actual(db_session, generation, {"seconds": 3})
        await settle_actual(db_session, generation, {"seconds": 3})
        await db_session.commit()
        await db_session.refresh(partner)
        assert partner.balance_rub == Decimal("140.00")
        ledger = await _entries(db_session, generation)
        assert sum((entry.amount_rub for entry in ledger), Decimal(0)) == Decimal("-60.00")
        assert {entry.operation_type for entry in ledger} == {
            "generation_reserve",
            "generation_reserve_release",
            "generation_late_charge",
            "generation_usage_adjustment",
        }
    finally:
        await provider.aclose()


async def test_legacy_generation_api_collects_exact_charge_only_after_completion(client, db_session, monkeypatch):
    def handler(request):
        if request.method == "POST":
            return httpx.Response(202, json={"request_id": "legacy-actual-only"})
        return httpx.Response(
            200,
            json={
                "request_id": "legacy-actual-only",
                "model": "seedance-2.5",
                "status": "done",
                "usage": {"billed_seconds": 4, "output_seconds": 4, "reference_video_seconds": 0},
            },
        )

    partner, headers, provider = await setup(
        db_session,
        monkeypatch,
        handler,
        model="seedance-2.5",
        category="video",
        rates=[("default", "480p", "second", Decimal("20"), Decimal("0.078"))],
    )
    partner.balance_rub = Decimal("90.00")
    await db_session.commit()
    monkeypatch.setattr(
        "app.generations.service.get_partner_provider_adapter",
        AsyncMock(return_value=ArgoLinkAdapter(api_key="upstream", client=provider)),
    )
    try:
        response = await client.post(
            "/api/v1/generations",
            headers=headers,
            json={
                "model_slug": "seedance-2.5",
                "prompt": "Slow motion",
                "resolution": "480p",
                "mode": "default",
                "duration_seconds": 4,
                "idempotency_key": "legacy-actual-only-idem",
            },
        )
        assert response.status_code == 202, response.text
        generation = await db_session.get(Generation, response.json()["id"])
        assert generation.partner_price_rub == Decimal("80.00")
        await db_session.refresh(partner)
        assert partner.balance_rub == Decimal("90.00")
        assert await _entries(db_session, generation) == []
        attempt = await dispatch_generation_to_provider(db_session, generation)
        assert attempt is not None
        attempt.next_poll_at = None
        await db_session.commit()
        await poll_generation_provider(db_session, generation)
        await db_session.commit()
        await db_session.refresh(partner)
        assert generation.status == "completed"
        assert generation.actual_charge_rub == Decimal("80.00")
        assert partner.balance_rub == Decimal("10.00")
        ledger = await _entries(db_session, generation)
        assert [(e.operation_type, e.amount_rub) for e in ledger] == [("generation_usage_charge", Decimal("-80.00"))]
        await poll_generation_provider(db_session, generation)
        assert len(await _entries(db_session, generation)) == 1
    finally:
        await provider.aclose()


async def test_old_untagged_partner_holds_are_not_mass_refunded(db_session):
    """Earlier APIX Seedance/Nano jobs are outside the approved Qwen refund."""
    from app.accounts.models import Partner
    from app.workers.generation_worker import release_stale_partner_reserves_once

    partner = Partner(
        telegram_id="old-partner-holds",
        company_name="Historical",
        project_name="Historical",
        balance_rub=Decimal("100"),
    )
    db_session.add(partner)
    await db_session.flush()
    generation = Generation(
        partner_id=partner.id,
        model_id="historical-video",
        model_slug="seedance-2.5",
        mode="videos/generations",
        resolution="720p",
        duration_seconds=4,
        status="reconciliation_required",
        prompt="Old request",
        idempotency_key="historical-hold-no-policy",
        created_at=utc_now() - timedelta(days=8),
        partner_price_rub=Decimal("80"),
        request_payload={},
    )
    db_session.add(generation)
    await db_session.flush()
    await apply_partner_balance_change(
        db_session,
        partner,
        Decimal("-80"),
        "generation_reserve",
        f"generation-reserve:{generation.id}",
        generation.id,
        "Old unresolved partner hold",
        allow_negative=False,
    )
    await db_session.commit()
    assert await release_stale_partner_reserves_once(db_session) == 0
    await db_session.commit()
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("20")
    assert [(entry.operation_type, entry.amount_rub) for entry in await _entries(db_session, generation)] == [
        ("generation_reserve", Decimal("-80"))
    ]
