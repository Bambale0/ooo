from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.catalog.models import PartnerPrice
from app.contracts.registry import validate_request, video_reserve_seconds
from app.inference.accounting import charges
from app.inference.service import quote
from app.infrastructure.config import get_settings

MODEL = "seedance-2.5"
FX = Decimal("83.296218")
COSTS = {"480p": Decimal(".0874"), "720p": Decimal(".196"), "1080p": Decimal(".483")}
FIXED = {"480p": Decimal("12"), "720p": Decimal("23.80"), "1080p": Decimal("57.13")}
VIDEO_INPUT_COSTS = {"480p": Decimal(".0523"), "720p": Decimal(".117"), "1080p": Decimal(".289")}


@pytest.fixture
def edit_rule(monkeypatch):
    monkeypatch.setenv("SEEDANCE_25_EDIT_MARKUP_RUB_PER_SECOND", "2.50")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def prices():
    return [
        PartnerPrice(
            mode=mode,
            resolution=tier,
            billing_unit="second",
            provider_cost_usdt=cost,
            price_rub=FIXED[tier],
        )
        for tier, cost in COSTS.items()
        for mode in ("default", "edit")
    ]


def edit_body(**changes):
    return {
        "model": MODEL,
        "prompt": "Make the colors warmer and keep everything else",
        "resolution": "720p",
        "omni_reference_task_type": "edit",
        "reference_videos": [{"url": "https://example.org/street.mp4"}],
        **changes,
    }


@pytest.mark.parametrize("tier", COSTS)
@pytest.mark.parametrize("fx", [FX, Decimal("100.123456")])
def test_edit_cost_plus_uses_current_fx_without_unit_rounding(edit_rule, tier, fx):
    rates, units, resolution = quote("videos/generations", edit_body(resolution=tier), prices(), fx=fx)
    expected = COSTS[tier] * fx + Decimal("2.50")
    assert rates == {"seconds": {"retail": str(expected), "cost": str(VIDEO_INPUT_COSTS[tier])}}
    assert units == {"seconds": 60}
    assert resolution == tier
    amount, cost = charges(rates, {"seconds": 20})
    assert amount == (expected * 20).quantize(Decimal(".01"), rounding="ROUND_HALF_UP")
    assert cost == VIDEO_INPUT_COSTS[tier] * 20


@pytest.mark.parametrize("mode", [None, "auto", "reference"])
@pytest.mark.parametrize("with_video", [False, True])
def test_non_edit_uses_video_input_tariff_only_when_video_present(edit_rule, mode, with_video):
    body = {"model": MODEL, "prompt": "A street", "duration": 10, "resolution": "720p"}
    if mode is not None:
        body["omni_reference_task_type"] = mode
    if with_video:
        body["reference_videos"] = [{"url": "https://example.org/street.mp4"}]
    rates, units, _ = quote("videos/generations", body, prices(), fx=FX)
    expected = COSTS["720p"] * FX + Decimal("2.50") if with_video else FIXED["720p"]
    assert Decimal(rates["seconds"]["retail"]) == expected
    assert Decimal(rates["seconds"]["cost"]) == (VIDEO_INPUT_COSTS["720p"] if with_video else COSTS["720p"])
    assert units == {"seconds": 40 if with_video else 10}


def test_other_seedance_model_does_not_get_edit_rule(edit_rule):
    rates, _, _ = quote("videos/generations", edit_body(model="seedance-2.5-self-developed-nsfw"), prices(), fx=FX)
    assert Decimal(rates["seconds"]["retail"]) == FIXED["720p"]


def test_unconfigured_edit_rule_keeps_existing_tariff(monkeypatch):
    monkeypatch.delenv("SEEDANCE_25_EDIT_MARKUP_RUB_PER_SECOND", raising=False)
    get_settings.cache_clear()
    try:
        rates, _, _ = quote("videos/generations", edit_body(), prices(), fx=FX)
        assert Decimal(rates["seconds"]["retail"]) == FIXED["720p"]
    finally:
        get_settings.cache_clear()


def test_enabled_edit_rule_requires_separate_procurement_row(edit_rule):
    with pytest.raises(HTTPException) as error:
        quote("videos/generations", edit_body(), [p for p in prices() if p.mode == "default"], fx=FX)
    assert error.value.status_code == 503


def test_edit_preserves_omitted_duration_and_ratio():
    body = edit_body()
    assert validate_request("videos/generations", body) == body
    assert video_reserve_seconds(body) == 60
    assert "duration" not in body and "aspect_ratio" not in body


@pytest.mark.parametrize(
    "changes",
    [
        {"reference_videos": [{"url": "https://example.org/a.mp4"}, {"url": "https://example.org/b.mp4"}]},
        {"reference_images": [{"url": "https://example.org/photo.jpg"}]},
        {"reference_audios": [{"url": "https://example.org/audio.mp3"}]},
        {"reference_videos": []},
        {"duration": 10},
        {"aspect_ratio": "16:9"},
        {"size": "1280x720"},
        {"prompt": ""},
    ],
)
def test_edit_rejects_unsupported_media_before_reserve(changes):
    with pytest.raises(ValueError):
        validate_request("videos/generations", edit_body(**changes))


def test_normal_reference_still_accepts_multiple_media():
    body = edit_body(
        omni_reference_task_type="reference",
        duration=10,
        aspect_ratio="16:9",
        reference_videos=[{"url": "https://example.org/a.mp4"}, {"url": "https://example.org/b.mp4"}],
        reference_images=[{"url": "https://example.org/photo.jpg"}],
        reference_audios=[{"url": "https://example.org/audio.mp3"}],
    )
    assert validate_request("videos/generations", body) == body
