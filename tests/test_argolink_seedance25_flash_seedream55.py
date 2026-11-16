"""ArgoLink Seedance 2.5 Flash and Seedream 5.5 reviewed contracts and billing."""

import base64
import io
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image
from sqlalchemy import select
from test_native_inference import setup

from app.catalog.models import Model
from app.catalog.seed_models_2026 import register_seed_models
from app.catalog.sync import variants
from app.catalog.video_edit_pricing import video_input_cost, video_pricing_mode
from app.contracts.registry import MODELS, validate_request, video_reserve_seconds
from app.generations.models import Generation
from app.inference.accounting import charges
from app.inference.images import image_usage
from app.inference.service import quote
from app.providers.argolink import ArgoLinkAdapter
from app.providers.models import ProviderModelCapability

FLASH = "seedance-2.5-flash"
DREAM = "seedream-5.5"


def _price(resolution, procurement, retail, *, mode="default", unit="generation"):
    return SimpleNamespace(
        resolution=resolution,
        provider_cost_usdt=Decimal(procurement),
        price_rub=Decimal(retail),
        mode=mode,
        billing_unit=unit,
    )


def _png(width=1088, height=1088):
    output = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(output, format="PNG")
    return {"b64_json": base64.b64encode(output.getvalue()).decode()}


def test_seedream_reviewed_profile_and_supplier_reference_fee():
    entry = MODELS[DREAM]
    assert entry["category"] == "image"
    assert entry["image"]["output_format"] == "PNG"
    assert entry["image"]["max_references"] == 8
    assert entry["image"]["default_resolution"] == "2k"
    assert entry["procurement"]["reference_image_price"] == Decimal("0.008")
    assert [(m, r, u, c) for m, r, u, c in variants(entry)] == [
        ("default", "1K", "generation", Decimal("0.02")),
        ("default", "2K", "generation", Decimal("0.035")),
        ("reference", "reference", "generation", Decimal("0.008")),
    ]
    request = {"model": DREAM, "prompt": "Keep composition", "resolution": "1k",
               "images": [{"image_url": "https://example.org/a.png"}], "n": 1}
    assert validate_request("images/edits", request) == request
    with pytest.raises(ValueError):
        validate_request("images/generations", {**request, "images": request["images"]})
    with pytest.raises(ValueError):
        validate_request("images/edits", {**request, "images": request["images"] * 9})


def test_seedream_reference_fee_is_reserved_and_settled_once():
    prices = [
        _price("1K", "0.02", "3.00"),
        _price("2K", "0.035", "5.00"),
        _price("reference", "0.008", "1.00", mode="reference"),
    ]
    request = {"model": DREAM, "prompt": "Recolor", "resolution": "1k",
               "images": [{"image_url": "https://example.org/one.png"},
                          {"image_url": "https://example.org/two.png"}]}
    rates, reserved, tier = quote("images/edits", request, prices, fx=Decimal("100"))
    assert tier == "1K"
    assert reserved == {"1K": 1, "reference_images": 2}
    assert charges(rates, reserved) == (Decimal("5.00"), Decimal("0.036"))
    actual = image_usage(request, {"data": [_png()]})
    assert actual == reserved
    assert charges(rates, actual) == (Decimal("5.00"), Decimal("0.036"))


def test_seedream_multipart_reference_count_uses_admitted_count():
    prices = [
        _price("1K", "0.02", "3.00"),
        _price("2K", "0.035", "5.00"),
        _price("reference", "0.008", "1.00", mode="reference"),
    ]
    request = {"model": DREAM, "prompt": "Edit", "resolution": "2k"}
    rates, reserved, tier = quote(
        "images/edits", request, prices, fx=Decimal("100"), reference_image_count=3,
    )
    assert tier == "2K"
    assert reserved == {"2K": 1, "reference_images": 3}
    assert charges(rates, reserved) == (Decimal("8.00"), Decimal("0.059"))
    assert image_usage(request, {"data": [_png(1984, 1984)]}, reference_image_count=3) == reserved


def test_seedream_does_not_charge_reference_on_text_to_image():
    prices = [
        _price("1K", "0.02", "3.00"),
        _price("2K", "0.035", "5.00"),
        _price("reference", "0.008", "1.00", mode="reference"),
    ]
    rates, units, _ = quote("images/generations", {"model": DREAM, "prompt": "Cup"}, prices, fx=Decimal("100"))
    assert units == {"2K": 1}
    assert charges(rates, units) == (Decimal("5.00"), Decimal("0.035"))


def test_seedance_flash_native_modes_and_procurement():
    entry = MODELS[FLASH]
    assert [t["label"] for t in entry["procurement"]["tiers"]] == ["480p", "720p"]
    assert entry["procurement"]["video_input_tiers"] == {
        "480p": Decimal("0.0351"), "720p": Decimal("0.0792"),
    }
    assert video_reserve_seconds({"model": FLASH, "prompt": "Mountain", "duration": 30}) == 30
    body = {"model": FLASH, "prompt": "Walk", "duration": 5, "resolution": "720p",
            "reference_audios": [{"url": "https://example.org/music.mp3"}],
            "generate_audio": False}
    assert validate_request("videos/generations", body) == body
    input_body = {"model": FLASH, "prompt": "Animate", "duration": 5, "resolution": "720p",
                  "reference_videos": [{"url": "https://example.org/movement.mp4"}]}
    assert validate_request("videos/generations", input_body) == input_body
    assert video_pricing_mode(input_body) == "video_input"
    assert video_input_cost(FLASH, input_body, "720p") == Decimal("0.0792")


@pytest.mark.parametrize("patch", [
    {"resolution": "1080p"},
    {"duration": 31},
    {"n": 2},
    {"seed": 1},
    {"watermark": True},
    {"reference_images": [{"url": "https://example.org/a.png"}] * 11},
    {"reference_videos": [{"url": "https://example.org/a.mp4"}] * 6},
    {"reference_audios": [{"url": "https://example.org/a.mp3"}] * 6},
    {"draft": True},
])
def test_seedance_flash_rejects_unsupported_inputs(patch):
    with pytest.raises(ValueError):
        validate_request(
            "videos/generations", {"model": FLASH, "prompt": "Ocean", "duration": 5, **patch},
        )


async def test_scoped_registration_remains_draft_and_is_idempotent(db_session):
    plan = await register_seed_models(db_session)
    await register_seed_models(db_session)
    for slug in (FLASH, DREAM):
        model = await db_session.scalar(select(Model).where(Model.slug == slug))
        assert model is not None and model.status == "draft"
        assert model.has_provider_integration is True
        assert model.has_successful_smoke is False
        capabilities = (await db_session.execute(
            select(ProviderModelCapability).where(
                ProviderModelCapability.model_id == model.id,
                ProviderModelCapability.provider == "argolink",
            )
        )).scalars().all()
        assert len(capabilities) == len(plan[slug]["required_prices"])
    flash_model = await db_session.scalar(select(Model).where(Model.slug == FLASH))
    assert {
        (c.mode, c.resolution) for c in (await db_session.execute(
            select(ProviderModelCapability).where(ProviderModelCapability.model_id == flash_model.id)
        )).scalars()
    } == {("default", "480p"), ("default", "720p"), ("video_input", "480p"), ("video_input", "720p")}


def test_seedance_flash_edit_omits_duration_and_can_take_audio_reference():
    edit = {
        "model": FLASH,
        "prompt": "Warm colors and softer audio",
        "omni_reference_task_type": "edit",
        "reference_videos": [{"url": "https://example.org/scene.mp4"}],
        "reference_audios": [{"url": "https://example.org/music.mp3"}],
        "resolution": "480p",
    }
    assert validate_request("videos/generations", edit) == edit
    assert video_reserve_seconds(edit) == 60
    with pytest.raises(ValueError):
        validate_request("videos/generations", {**edit, "duration": -1})
    with pytest.raises(ValueError):
        validate_request("videos/generations", {**edit, "duration": 5})


def test_seedance_flash_extend_bills_reference_video():
    request = {
        "model": FLASH,
        "prompt": "Continue the view along the coast",
        "omni_reference_task_type": "extend",
        "duration": 10,
        "reference_videos": [{"url": "https://example.org/scene.mp4"}],
        "resolution": "720p",
    }
    assert validate_request("videos/generations", request) == request
    assert video_pricing_mode(request) == "video_input"
    assert video_input_cost(FLASH, request, "720p") == Decimal("0.0792")
    assert video_reserve_seconds(request) == 40


def test_seedance_flash_rejects_unpriced_1080_and_draft_controls():
    with pytest.raises(ValueError):
        validate_request("videos/generations", {"model": FLASH, "prompt": "Sunrise", "resolution": "1080p"})
    with pytest.raises(ValueError):
        validate_request("videos/generations", {"model": FLASH, "prompt": "Sunrise", "draft": True})


async def test_seedream_native_edit_settles_output_and_two_references_once(client, db_session, monkeypatch):
    submitted = []

    def provider(request):
        submitted.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [_png(1984, 1984)]})

    rates = [
        ("default", "1K", "generation", Decimal("3"), Decimal("0.02")),
        ("default", "2K", "generation", Decimal("5"), Decimal("0.035")),
        ("reference", "reference", "generation", Decimal("1"), Decimal("0.008")),
    ]
    partner, headers, upstream = await setup(
        db_session, monkeypatch, provider, model=DREAM, category="image", rates=rates,
    )
    body = {
        "model": DREAM,
        "prompt": "Turn the photo into an illustration",
        "resolution": "2k",
        "images": [
            {"image_url": "https://example.org/ref1.png"},
            {"image_url": "https://example.org/ref2.png"},
        ],
    }
    try:
        response = await client.post("/v1/images/edits", json=body, headers=headers)
        assert response.status_code == 200, response.text
        replay = await client.post("/v1/images/edits", json=body, headers=headers)
        assert replay.status_code == 200 and replay.content == response.content
        assert submitted == [body]
        generation = await db_session.scalar(select(Generation).where(Generation.partner_id == partner.id))
        assert generation.usage_snapshot == {"2K": 1, "reference_images": 2}
        assert generation.actual_charge_rub == Decimal("7.00")
        assert generation.actual_provider_cost_usdt == Decimal("0.051")
        await db_session.refresh(partner)
        assert partner.balance_rub == Decimal("999993.00")
    finally:
        await upstream.aclose()


async def test_seedream_native_failure_does_not_charge_references(client, db_session, monkeypatch):
    rates = [
        ("default", "1K", "generation", Decimal("3"), Decimal("0.02")),
        ("default", "2K", "generation", Decimal("5"), Decimal("0.035")),
        ("reference", "reference", "generation", Decimal("1"), Decimal("0.008")),
    ]
    partner, headers, upstream = await setup(
        db_session, monkeypatch,
        lambda request: httpx.Response(502, json={"error": "unavailable"}),
        model=DREAM, category="image", rates=rates,
    )
    try:
        response = await client.post("/v1/images/edits", headers=headers, json={
            "model": DREAM,
            "prompt": "Edit",
            "images": [{"image_url": "https://example.org/ref.png"}],
        })
        assert response.status_code >= 400
        await db_session.refresh(partner)
        assert partner.balance_rub == Decimal("1000000")
    finally:
        await upstream.aclose()


async def test_seedream_native_multipart_ref_cost_uses_file_count(client, db_session, monkeypatch):
    count = 3
    submitted = []

    def provider(request):
        submitted.append(request)
        assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
        assert request.content.count(b'name="image[]"') == count
        return httpx.Response(200, json={"data": [_png()]})

    rates = [
        ("default", "1K", "generation", Decimal("3"), Decimal("0.02")),
        ("default", "2K", "generation", Decimal("5"), Decimal("0.035")),
        ("reference", "reference", "generation", Decimal("1"), Decimal("0.008")),
    ]
    partner, headers, upstream = await setup(
        db_session, monkeypatch, provider, model=DREAM, category="image", rates=rates,
    )
    reference = base64.b64decode(_png()["b64_json"])
    try:
        response = await client.post(
            "/v1/images/edits",
            headers=headers,
            data={"model": DREAM, "prompt": "Change the style", "resolution": "1k"},
            files=[("image[]", (f"ref-{i}.png", reference, "image/png")) for i in range(count)],
        )
        assert response.status_code == 200, response.text
        assert len(submitted) == 1
        generation = await db_session.scalar(select(Generation).where(Generation.partner_id == partner.id))
        assert generation.usage_snapshot == {"1K": 1, "reference_images": count}
        assert generation.actual_charge_rub == Decimal("6.00")
        assert generation.actual_provider_cost_usdt == Decimal("0.044")
        await db_session.refresh(partner)
        assert partner.balance_rub == Decimal("999994.00")
    finally:
        await upstream.aclose()


async def test_seedance_flash_native_reference_video_bills_reported_seconds(client, db_session, monkeypatch):
    from app.generations.service import dispatch_generation_to_provider, poll_generation_provider

    submitted = []

    def provider(request):
        if request.method == "POST":
            submitted.append(json.loads(request.content))
            return httpx.Response(202, json={"request_id": "flash-upstream-job"})
        return httpx.Response(
            200,
            json={
                "status": "done",
                "model": FLASH,
                "video": {"url": "https://argolink.io/v1/videos/flash-upstream-job/content"},
                "usage": {"output_seconds": 5, "reference_video_seconds": 5, "billed_seconds": 10},
            },
        )

    rates = [
        ("default", "480p", "second", Decimal("8"), Decimal("0.0572")),
        ("default", "720p", "second", Decimal("16"), Decimal("0.129")),
        ("video_input", "480p", "second", Decimal("5"), Decimal("0.0351")),
        ("video_input", "720p", "second", Decimal("10"), Decimal("0.0792")),
    ]
    partner, headers, upstream = await setup(
        db_session, monkeypatch, provider, model=FLASH, category="video", rates=rates,
    )
    monkeypatch.setattr(
        "app.generations.service.get_partner_provider_adapter",
        AsyncMock(return_value=ArgoLinkAdapter(api_key="upstream", client=upstream)),
    )
    body = {
        "model": FLASH,
        "prompt": "Continue the camera movement",
        "duration": 5,
        "resolution": "720p",
        "reference_videos": [{"url": "https://example.org/reference.mp4"}],
        "reference_audios": [{"url": "https://example.org/sound.mp3"}],
    }
    try:
        response = await client.post("/v1/videos/generations", json=body, headers=headers)
        assert response.status_code == 202, response.text
        generation = await db_session.get(Generation, response.json()["request_id"])
        assert generation.partner_price_rub == Decimal("350")
        await dispatch_generation_to_provider(db_session, generation)
        await db_session.commit()
        await poll_generation_provider(db_session, generation)
        assert submitted == [body] and generation.status == "completed"
        assert generation.actual_charge_rub == Decimal("100")
        assert generation.actual_provider_cost_usdt == Decimal("0.792")
        await db_session.refresh(partner)
        assert partner.balance_rub == Decimal("999900")
    finally:
        await upstream.aclose()
