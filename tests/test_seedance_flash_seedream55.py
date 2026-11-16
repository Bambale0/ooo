"""Contract and accounting regressions for the 2026-11 ArgoLink Seed additions."""

import base64
import io
import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import select
from test_native_inference import setup

from app.catalog.sync import effective_procurement, variants
from app.contracts.registry import MODELS, validate_request, video_reserve_seconds
from app.inference.accounting import charges
from app.inference.images import image_usage
from app.inference.service import quote
from app.generations.models import Generation


def price(mode, resolution, unit, retail, cost):
    return SimpleNamespace(
        mode=mode, resolution=resolution, billing_unit=unit,
        price_rub=Decimal(retail), provider_cost_usdt=Decimal(cost)
    )


def test_seedance_flash_model_contract_and_cost_schedule():
    entry = MODELS["seedance-2.5-flash"]
    assert entry["category"] == "video"
    assert entry["endpoint"] == "/v1/videos/generations"
    assert list(variants(entry)) == [
        ("default", "480p", "second", Decimal("0.0572")),
        ("default", "720p", "second", Decimal("0.129")),
        ("video_input", "480p", "second", Decimal("0.0351")),
        ("video_input", "720p", "second", Decimal("0.0792")),
    ]
    live = {
        "id": "seedance-2.5-flash",
        "pricing": {"effective": {
            "currency": "USD", "billing_mode": "video", "unit": "second",
            "generation_per_unit": Decimal("0.0572"),
            "tiers": [{"label": "480p", "price": Decimal("0.0572")},
                      {"label": "720p", "price": Decimal("0.129")}],
            "reference_video_multiplier": Decimal("0.6143"),
        }},
    }
    assert effective_procurement(live) == entry["procurement"]


@pytest.mark.parametrize("duration", [4, 5, 30])
def test_seedance_flash_duration_and_reference_limits(duration):
    body = {"model": "seedance-2.5-flash", "prompt": "A tree in the wind", "duration": duration}
    assert validate_request("videos/generations", body) == body
    for media, limit in (("reference_images", 10), ("reference_videos", 5), ("reference_audios", 5)):
        request = {**body, media: [{"url": "https://media.example.org/a.mp4"}] * limit}
        assert validate_request("videos/generations", request) == request
        with pytest.raises(ValueError, match="reference_limit"):
            validate_request("videos/generations", {**body, media: request[media] * 2})


def test_seedance_flash_edit_frames_and_video_input_billable_seconds():
    media = {"url": "https://media.example.org/a.mp4"}
    edit = {
        "model": "seedance-2.5-flash",
        "prompt": "Change the color of the sky",
        "omni_reference_task_type": "edit",
        "reference_videos": [media],
        "resolution": "480p",
    }
    assert validate_request("videos/generations", edit) == edit
    assert video_reserve_seconds(edit) == 60
    frames = {"model": "seedance-2.5-flash", "prompt": "Zoom in",
              "start_image": media, "aspect_ratio": "adaptive"}
    assert validate_request("videos/generations", frames) == frames
    with pytest.raises(ValueError):
        validate_request("videos/generations", {**frames, "aspect_ratio": "16:9"})
    with pytest.raises(ValueError):
        validate_request("videos/generations", {**frames, "draft": True})
    with pytest.raises(ValueError):
        validate_request("videos/generations", {**frames, "resolution": "1080p"})


def test_seedance_flash_reference_video_uses_separate_pricing_row():
    body = {"model": "seedance-2.5-flash", "prompt": "Follow the subject",
            "duration": 5, "resolution": "480p",
            "reference_videos": [{"url": "https://media.example.org/source.mp4"}]}
    prices = [price("default", "480p", "second", "6", "0.0572"),
              price("default", "720p", "second", "14", "0.129"),
              price("video_input", "480p", "second", "4", "0.0351"),
              price("video_input", "720p", "second", "9", "0.0792")]
    rates, units, tier = quote("videos/generations", body, prices, fx=Decimal("90"))
    assert (tier, units) == ("480p", {"seconds": 35})
    assert rates["seconds"] == {"retail": "4", "cost": "0.0351"}


def test_seedream55_contract_profile_and_reference_price():
    entry = MODELS["seedream-5.5"]
    assert entry["image"]["output_format"] == "PNG"
    assert entry["image"]["max_references"] == 8
    assert entry["image"]["default_resolution"] == "2k"
    assert list(variants(entry)) == [
        ("default", "1K", "generation", Decimal("0.02")),
        ("default", "2K", "generation", Decimal("0.035")),
        ("reference_image", "default", "image_reference", Decimal("0.008")),
    ]


def test_seedream55_reference_surcharge_is_reserved_and_settled():
    body = {
        "model": "seedream-5.5", "prompt": "Change the background",
        "images": [{"image_url": "https://media.example.org/a.png"}] * 2,
    }
    assert validate_request("images/edits", body) == body
    prices = [
        price("default", "1K", "generation", "3.00", "0.02"),
        price("default", "2K", "generation", "5.00", "0.035"),
        price("reference_image", "default", "image_reference", "1.00", "0.008"),
    ]
    rates, reserved, tier = quote("images/edits", body, prices, fx=Decimal("90"))
    assert tier == "2K"
    assert reserved == {"2K": 1, "reference_images": 2}
    assert charges(rates, reserved) == (Decimal("7.00"), Decimal("0.051000000000000000"))

    out = io.BytesIO()
    Image.new("RGB", (1984, 1984), "green").save(out, format="PNG")
    data = {"data": [{"b64_json": base64.b64encode(out.getvalue()).decode()}]}
    actual = image_usage(body, data)
    assert actual == reserved
    assert charges(rates, actual) == charges(rates, reserved)


def test_seedream55_multipart_reference_count_and_fail_closed_pricing():
    body = {"model": "seedream-5.5", "prompt": "Edit", "resolution": "1k"}
    assert validate_request("images/edits", body, multipart=True) == body
    prices = [
        price("default", "1K", "generation", "3.00", "0.02"),
        price("default", "2K", "generation", "5.00", "0.035"),
    ]
    with pytest.raises(HTTPException, match="provider_temporarily_unavailable"):
        quote("images/edits", body, prices, fx=Decimal("90"), reference_count=3)
    prices.append(price("reference_image", "default", "image_reference", "1.00", "0.008"))
    rates, units, tier = quote("images/edits", body, prices, fx=Decimal("90"), reference_count=3)
    assert units == {"1K": 1, "reference_images": 3}
    assert rates["reference_images"]["cost"] == "0.008"


@pytest.mark.parametrize("references", [1, 3])
async def test_seedream55_json_edit_idempotency_and_real_settlement(
    client, db_session, monkeypatch, references
):
    """The public endpoint and the ledger must agree on the paid reference count."""
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        out = io.BytesIO()
        Image.new("RGB", (1088, 1088), "green").save(out, format="PNG")
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(out.getvalue()).decode()}]})

    rates = [
        ("default", "1K", "generation", Decimal("3.00"), Decimal("0.02")),
        ("default", "2K", "generation", Decimal("5.00"), Decimal("0.035")),
        ("reference_image", "default", "image_reference", Decimal("1.00"), Decimal("0.008")),
    ]
    partner, headers, upstream = await setup(
        db_session, monkeypatch, handler, model="seedream-5.5", category="image", rates=rates
    )
    body = {"model": "seedream-5.5", "prompt": "Change background", "resolution": "1k",
            "images": [{"image_url": f"https://example.org/{index}.png"} for index in range(references)]}
    try:
        response = await client.post("/v1/images/edits", headers=headers, json=body)
        assert response.status_code == 200, response.text
        repeated = await client.post("/v1/images/edits", headers=headers, json=body)
        assert repeated.status_code == 200 and repeated.content == response.content
        generation = await db_session.scalar(select(Generation))
        assert generation.usage_snapshot == {"1K": 1, "reference_images": references}
        assert generation.actual_provider_cost_usdt == Decimal("0.02") + Decimal("0.008") * references
        expected = Decimal("3.00") + references * Decimal("1.00")
        assert generation.actual_charge_rub == expected
        assert partner.balance_rub == Decimal("1000000") - expected
        assert calls == [body]
    finally:
        await upstream.aclose()


async def test_seedream55_multipart_edits_count_all_image_files(
    client, db_session, monkeypatch
):
    seen = []

    def handler(request):
        seen.append(request.content.count(b'name="image[]"'))
        out = io.BytesIO()
        Image.new("RGB", (1088, 1088), "blue").save(out, format="PNG")
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(out.getvalue()).decode()}]})

    rates = [
        ("default", "1K", "generation", Decimal("3.00"), Decimal("0.02")),
        ("default", "2K", "generation", Decimal("5.00"), Decimal("0.035")),
        ("reference_image", "default", "image_reference", Decimal("1.00"), Decimal("0.008")),
    ]
    partner, headers, upstream = await setup(
        db_session, monkeypatch, handler, model="seedream-5.5", category="image", rates=rates
    )
    file_bytes = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(file_bytes, format="PNG")
    try:
        response = await client.post(
            "/v1/images/edits",
            headers=headers,
            data={"model": "seedream-5.5", "prompt": "Change color", "resolution": "1k"},
            files=[
                ("image[]", (f"{i}.png", file_bytes.getvalue(), "image/png"))
                for i in range(2)
            ],
        )
        assert response.status_code == 200, response.text
        generation = await db_session.scalar(select(Generation))
        assert generation.usage_snapshot == {"1K": 1, "reference_images": 2}
        assert generation.actual_provider_cost_usdt == Decimal("0.036")
        assert generation.actual_charge_rub == Decimal("5.00")
        assert partner.balance_rub == Decimal("999995.00")
        assert seen == [2]
    finally:
        await upstream.aclose()
