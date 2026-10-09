from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select
from test_native_inference import setup

from app.api.native_video_reference import EXAMPLES, REVIEWED_VIDEO_MODELS, render_native_video_reference
from app.billing.models import LedgerEntry
from app.catalog.models import Model
from app.contracts.registry import MODELS, validate_request, video_reserve_seconds
from app.generations.models import Generation
from app.generations.service import dispatch_generation_to_provider, poll_generation_provider
from app.infrastructure.config import get_settings
from app.providers.argolink import ArgoLinkAdapter
from app.providers.base import ProviderGenerationRequest
from app.providers.video_contract import video_request_body

CASES = [(slug, tier["label"], tier["price"])
         for slug in sorted(REVIEWED_VIDEO_MODELS) for tier in MODELS[slug]["procurement"]["tiers"]]


@pytest.mark.parametrize("slug,tier,cost", CASES)
@pytest.mark.parametrize("outcome", ["done", "failed"])
async def test_native_video_reference_accounting_and_idempotency(
    client, db_session, monkeypatch, slug, tier, cost, outcome
):
    submissions = []

    def handler(request):
        if request.method == "POST":
            submissions.append(request.content)
            return httpx.Response(202, json={"request_id": "safe-video-task"})
        body = {"request_id": "safe-video-task", "model": slug, "status": outcome}
        if outcome == "done":
            body["usage"] = {"output_seconds": 4, "reference_video_seconds": 6, "billed_seconds": 10}
        else:
            body["error"] = {"code": "invalid_input", "message": "The source video is invalid."}
        return httpx.Response(200, json=body)

    monkeypatch.setattr(get_settings(), "rub_per_usdt", Decimal("84.50"))
    retail = ({"768p": Decimal("4.88"), "2k": Decimal("6.99")}[tier]
              if slug == "minimax-h3" else Decimal("25"))
    partner, headers, upstream = await setup(
        db_session, monkeypatch, handler, model=slug, category="video",
        rates=[("default", tier, "second", retail, cost)],
    )
    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter",
                        AsyncMock(return_value=ArgoLinkAdapter(api_key="upstream", client=upstream)))
    body = {"model": slug, "prompt": "Follow @Video 1", "resolution": tier, "duration": 4,
            "reference_videos": [{"url": "https://media.example.org/source.mp4"}]}
    try:
        response = await client.post("/v1/videos/generations", headers=headers, json=body)
        assert response.status_code == 202, response.text
        generation = await db_session.get(Generation, response.json()["request_id"])
        assert generation.partner_price_rub == retail * video_reserve_seconds(body)
        duplicate = await client.post("/v1/videos/generations", headers=headers, json=body)
        assert duplicate.status_code == 202
        assert duplicate.json()["request_id"] == generation.id
        attempt = await dispatch_generation_to_provider(db_session, generation)
        attempt.next_poll_at = None
        await db_session.commit()
        await poll_generation_provider(db_session, generation)
        await db_session.commit()
        entries = list(await db_session.scalars(select(LedgerEntry).where(LedgerEntry.generation_id == generation.id)))
        expected = retail * 10 if outcome == "done" else Decimal(0)
        assert sum((row.amount_rub for row in entries), Decimal(0)) == -expected
        if outcome == "done":
            assert generation.status == "completed"
            assert generation.actual_charge_rub == expected
            assert generation.actual_provider_cost_usdt == cost * 10
        else:
            assert generation.status == "failed"
            assert any(row.operation_type == "generation_reserve_release" for row in entries)
        await poll_generation_provider(db_session, generation)
        await db_session.refresh(partner)
        assert partner.balance_rub == Decimal("1000000") - expected
        after = list(await db_session.scalars(select(LedgerEntry).where(LedgerEntry.generation_id == generation.id)))
        assert len(after) == len(entries)
        assert len(submissions) == 1
    finally:
        await upstream.aclose()


@pytest.mark.parametrize("slug", sorted(REVIEWED_VIDEO_MODELS))
def test_native_wire_body_and_legacy_dto_use_reviewed_contract(slug):
    tier = "768p" if slug == "minimax-h3" else "720p"
    for body in EXAMPLES[slug]:
        assert validate_request("videos/generations", body) == body
        dto = ProviderGenerationRequest(generation_id="test", model_slug=slug, mode="videos/generations",
                                        resolution=tier, prompt="test", native_body=body)
        assert video_request_body(dto) == body
    legacy = ProviderGenerationRequest(generation_id="test", model_slug=slug, mode="default",
                                       resolution=tier, prompt="Animate", duration_seconds=5)
    assert video_request_body(legacy)["model"] == slug


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_public_docs_only_render_enabled_reviewed_models(client, db_session, lang):
    assert render_native_video_reference(lang, set()) == ""
    db_session.add_all([
        Model(slug="minimax-h3", name="MiniMax H3", modality="video", status="production"),
        Model(slug="wan-3-prime", name="Wan 3 Prime", modality="video", status="draft"),
    ])
    await db_session.commit()
    response = await client.get("/docs", params={"lang": lang})
    assert response.status_code == 200
    assert 'id="minimax-h3"' in response.text
    assert 'id="wan-3-prime"' not in response.text
    assert 'id="wan-3"' not in response.text
    assert "procurement" not in response.text
    assert "0.039" not in response.text
