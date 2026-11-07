import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select
from test_native_inference import setup

from app.api.native_video_reference import EXAMPLES, LIMITS, REVIEWED_VIDEO_MODELS, render_native_video_reference
from app.billing.models import LedgerEntry
from app.catalog.models import PartnerPrice
from app.catalog.sync import variants
from app.catalog.video_edit_pricing import video_pricing_mode
from app.contracts.registry import MODELS, validate_request
from app.generations.models import Generation
from app.generations.service import dispatch_generation_to_provider, poll_generation_provider
from app.inference.service import quote
from app.infrastructure.config import get_settings
from app.providers.argolink import ArgoLinkAdapter
from app.providers.models import ProviderModelCapability

MODEL = "kling-4.0"
PROTOCOL = "videos/generations"


def test_kling_4_reviewed_contract_and_text_request():
    entry = MODELS[MODEL]

    assert entry["category"] == "video"
    assert entry["endpoint"] == "/v1/videos/generations"
    assert {tier["label"]: tier["price"] for tier in entry["procurement"]["tiers"]} == {
        "720p": Decimal("0.1256"),
        "1080p": Decimal("0.188"),
    }
    assert entry["procurement"]["video_input_tiers"] == {
        "720p": Decimal("0.0754"),
        "1080p": Decimal("0.113"),
    }

    body = {
        "model": MODEL,
        "prompt": "A slow aerial shot over a foggy pine forest at sunrise",
        "duration": 5,
        "resolution": "720p",
        "aspect_ratio": "16:9",
    }
    assert validate_request(PROTOCOL, body) == body


def test_kling_4_catalog_has_separate_text_and_video_input_prices():
    assert set(variants(MODELS[MODEL])) == {
        ("default", "720p", "second", Decimal("0.1256")),
        ("default", "1080p", "second", Decimal("0.188")),
        ("video_input", "720p", "second", Decimal("0.0754")),
        ("video_input", "1080p", "second", Decimal("0.113")),
    }


def test_kling_4_video_reference_selects_its_own_partner_and_procurement_rate():
    prices = [
        SimpleNamespace(
            mode=mode,
            resolution="720p",
            billing_unit="second",
            price_rub=retail,
            provider_cost_usdt=cost,
        )
        for mode, retail, cost in (
            ("default", Decimal("20"), Decimal("0.1256")),
            ("video_input", Decimal("15"), Decimal("0.0754")),
        )
    ]
    text = {"model": MODEL, "prompt": "A forest", "duration": 5, "resolution": "720p"}
    reference = {
        **text,
        "reference_videos": [{"url": "https://media.example.org/motion.mp4"}],
    }

    assert video_pricing_mode(text) == "default"
    assert video_pricing_mode(reference) == "video_input"
    assert quote(PROTOCOL, text, prices, fx=Decimal("100"))[0]["seconds"] == {
        "retail": "20",
        "cost": "0.1256",
    }
    rates, units, resolution = quote(PROTOCOL, reference, prices, fx=Decimal("100"))
    assert rates["seconds"] == {"retail": "15", "cost": "0.0754"}
    assert units == {"seconds": 35}
    assert resolution == "720p"


def test_kling_4_reference_contract_preserves_supported_controls_and_rejects_unsupported_ones():
    body = {
        "model": MODEL,
        "prompt": "Let @Image 1 follow the motion from @Video 1",
        "duration": 30,
        "resolution": "1080p",
        "aspect_ratio": "adaptive",
        "reference_images": [{"url": f"https://media.example.org/image-{index}.png"} for index in range(10)],
        "reference_videos": [{"url": f"https://media.example.org/video-{index}.mp4"} for index in range(5)],
        "generate_audio": False,
        "n": 1,
    }
    assert validate_request(PROTOCOL, body) == body

    invalid_controls = (
        {"duration": 2},
        {"duration": 31},
        {"resolution": "480p"},
        {"aspect_ratio": "adaptive"},
        {"aspect_ratio": "4:3"},
        {"reference_images": [{"url": "https://media.example.org/image.png"}] * 11},
        {"reference_videos": [{"url": "https://media.example.org/video.mp4"}] * 6},
        {"reference_audios": [{"url": "https://media.example.org/audio.mp3"}]},
        {"start_image": {"url": "https://media.example.org/start.png"}},
        {"end_image": {"url": "https://media.example.org/end.png"}},
        {"generate_audio": "false"},
        {"seed": 42},
        {"watermark": True},
        {"n": 2},
    )
    base = {"model": MODEL, "prompt": "A slow camera move"}
    for controls in invalid_controls:
        with pytest.raises(ValueError):
            validate_request(PROTOCOL, {**base, **controls})

    with pytest.raises(ValueError, match="prompt_too_long"):
        validate_request(PROTOCOL, {"model": MODEL, "prompt": "я" * 11001})


async def test_kling_4_native_request_is_idempotent_and_settles_confirmed_video_usage(
    client, db_session, monkeypatch
):
    submitted = []

    def handler(request):
        if request.method == "POST":
            submitted.append(json.loads(request.content))
            return httpx.Response(202, json={"request_id": "private-kling-task"})
        return httpx.Response(
            200,
            json={
                "request_id": "private-kling-task",
                "model": MODEL,
                "status": "done",
                "usage": {"output_seconds": 5, "reference_video_seconds": 3, "billed_seconds": 8},
            },
        )

    monkeypatch.setattr(get_settings(), "rub_per_usdt", Decimal("100"))
    partner, headers, upstream = await setup(
        db_session,
        monkeypatch,
        handler,
        model=MODEL,
        category="video",
        rates=[
            ("default", "720p", "second", Decimal("20"), Decimal("0.1256")),
            ("video_input", "720p", "second", Decimal("15"), Decimal("0.0754")),
        ],
    )
    monkeypatch.setattr(
        "app.generations.service.get_partner_provider_adapter",
        AsyncMock(return_value=ArgoLinkAdapter(api_key="upstream", client=upstream)),
    )
    body = {
        "model": MODEL,
        "prompt": "Follow the movement from @Video 1",
        "duration": 5,
        "resolution": "720p",
        "reference_videos": [{"url": "https://media.example.org/motion.mp4"}],
        "generate_audio": False,
    }
    try:
        accepted = await client.post("/v1/videos/generations", json=body, headers=headers)
        assert accepted.status_code == 202, accepted.text
        duplicate = await client.post("/v1/videos/generations", json=body, headers=headers)
        assert duplicate.status_code == 202
        assert duplicate.json()["request_id"] == accepted.json()["request_id"]

        generation = await db_session.get(Generation, accepted.json()["request_id"])
        assert generation.partner_price_rub == Decimal("525")
        assert generation.provider_cost_usdt_snapshot == Decimal("2.639")
        attempt = await dispatch_generation_to_provider(db_session, generation)
        attempt.next_poll_at = None
        await db_session.commit()
        await poll_generation_provider(db_session, generation)
        await db_session.commit()

        assert submitted == [body]
        assert generation.status == "completed"
        assert generation.actual_charge_rub == Decimal("120")
        assert generation.actual_provider_cost_usdt == Decimal("0.6032")
        await db_session.refresh(partner)
        assert partner.balance_rub == Decimal("999880")
        entries = list(await db_session.scalars(select(LedgerEntry).where(LedgerEntry.generation_id == generation.id)))
        assert len(entries) == 1
    finally:
        await upstream.aclose()


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_kling_4_public_reference_is_self_contained_and_matches_runtime(lang):
    assert MODEL in REVIEWED_VIDEO_MODELS
    assert MODEL in LIMITS
    assert EXAMPLES[MODEL]
    for body in EXAMPLES[MODEL]:
        assert validate_request(PROTOCOL, body) == body

    rendered = render_native_video_reference(lang, {MODEL})
    assert f'id="{MODEL}"' in rendered
    assert all(value in rendered for value in ("3", "30", "720p", "1080p", "10", "5", "15"))
    assert "reference_audios" in rendered
    assert "generate_audio" in rendered


async def test_kling_4_cannot_be_enabled_with_an_incomplete_price_schedule(client, admin_headers):
    created = await client.post(
        "/api/v1/catalog/models",
        headers=admin_headers,
        json={
            "slug": MODEL,
            "name": "Kling 4.0",
            "modality": "video",
            "status": "draft",
            "has_provider_integration": True,
            "has_public_docs": True,
            "has_successful_smoke": True,
        },
    )
    assert created.status_code == 201
    price = await client.put(
        "/api/v1/catalog/pricing",
        headers=admin_headers,
        json={
            "model_slug": MODEL,
            "mode": "default",
            "resolution": "720p",
            "price_rub": "20.00",
            "provider_cost_usdt": "0.1256",
            "billing_unit": "second",
        },
    )
    assert price.status_code == 204

    enabled = await client.post(f"/api/v1/catalog/models/{MODEL}/enable", headers=admin_headers)
    assert enabled.status_code == 409
    assert enabled.json()["detail"] == "complete_price_schedule_required"

    for mode, resolution, retail, cost in (
        ("default", "1080p", "25.00", "0.188"),
        ("video_input", "720p", "15.00", "0.0754"),
        ("video_input", "1080p", "20.00", "0.113"),
    ):
        price = await client.put(
            "/api/v1/catalog/pricing",
            headers=admin_headers,
            json={
                "model_slug": MODEL,
                "mode": mode,
                "resolution": resolution,
                "price_rub": retail,
                "provider_cost_usdt": cost,
                "billing_unit": "second",
            },
        )
        assert price.status_code == 204

    enabled = await client.post(f"/api/v1/catalog/models/{MODEL}/enable", headers=admin_headers)
    assert enabled.status_code == 200
    assert enabled.json()["status"] == "production"


async def test_kling_4_scoped_registration_is_repeatable_and_leaves_money_and_enablement_to_admin(db_session):
    from app.catalog.kling_4 import register_kling_4

    model = await register_kling_4(db_session)
    await db_session.flush()

    assert model.slug == MODEL
    assert model.modality == "video"
    assert model.status == "draft"
    assert model.has_provider_integration is True
    assert model.has_public_docs is True
    assert model.has_successful_smoke is False
    assert list(await db_session.scalars(select(PartnerPrice).where(PartnerPrice.model_id == model.id))) == []
    capabilities = list(
        await db_session.scalars(
            select(ProviderModelCapability).where(ProviderModelCapability.model_id == model.id)
        )
    )
    assert {(item.mode, item.resolution) for item in capabilities} == {
        ("default", "720p"),
        ("default", "1080p"),
        ("video_input", "720p"),
        ("video_input", "1080p"),
    }

    repeated = await register_kling_4(db_session)
    await db_session.flush()
    assert repeated.id == model.id
    assert len(
        list(
            await db_session.scalars(
                select(ProviderModelCapability).where(ProviderModelCapability.model_id == model.id)
            )
        )
    ) == 4
