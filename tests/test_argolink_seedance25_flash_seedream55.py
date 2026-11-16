"""ArgoLink Seedance 2.5 Flash and Seedream 5.5 reviewed contracts and billing."""

import base64
import io
from decimal import Decimal
from types import SimpleNamespace

import pytest
from PIL import Image

from app.catalog.sync import variants
from app.catalog.video_edit_pricing import video_input_cost, video_pricing_mode
from app.contracts.registry import MODELS, validate_request, video_reserve_seconds
from app.inference.accounting import charges
from app.inference.images import image_usage
from app.inference.service import quote
from app.providers.base import ProviderGenerationRequest
from app.providers.video_contract import video_request_body

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
