import copy
from decimal import Decimal

import pytest

from app.catalog.sync import variants
from app.contracts.registry import MODELS, TEXT_PROTOCOLS, normalized_video, validate_request, video_reserve_seconds
from app.inference.streams import SSEDecoder, event_data


@pytest.mark.parametrize("model", list(MODELS))
def test_every_catalog_model_has_valid_native_request_and_decimal_procurement(model):
    entry = MODELS[model]
    if entry["category"] == "chat":
        for protocol in TEXT_PROTOCOLS:
            body = (
                {"model": model, "max_tokens": 128, "input": "hello"}
                if protocol == "responses"
                else {"model": model, "max_tokens": 128, "messages": [{"role": "user", "content": "hello"}]}
            )
            body["tools"] = [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}]
            body["thinking"] = {"type": "adaptive"}
            original = copy.deepcopy(body)
            assert validate_request(protocol, body) == original
            assert body == original
    elif entry["category"] == "image":
        for protocol in ("images/generations", "images/edits"):
            body = {
                "model": model,
                "prompt": "Edit reference",
                "images": [{"image_url": {"url": "https://example.com/image.png"}}],
            }
            assert validate_request(protocol, body) == body
    else:
        body = {"model": model, "prompt": "Animate", "duration": 5, "resolution": "720p"}
        assert validate_request("videos/generations", body) == body
    assert all(isinstance(v[3], Decimal) for v in variants(entry))


@pytest.mark.parametrize(
    "model,images,videos,audios,total",
    [
        ("seedance-2.5", 30, 10, 10, 50),
        ("seedance-2.0", 9, 3, 3, 12),
        ("seedance-2.0-mini", 9, 3, 3, 12),
        ("seedance-2.0-fast", 9, 3, 3, 12),
        ("wan-3", 10, 5, 5, 20),
    ],
)
def test_video_asset_limits_and_billable_references(model, images, videos, audios, total):
    body = {
        "model": model,
        "prompt": "References",
        "duration": 5,
        "resolution": "720p",
        "reference_images": [{"url": "https://example.com/image.png"}] * images,
        "reference_videos": [{"url": "https://example.com/video.mp4"}] * videos,
    }
    assert validate_request("videos/generations", body) == body
    assert video_reserve_seconds(body) == (35 if model == "seedance-2.5" else 20)
    body["reference_images"] *= 2
    with pytest.raises(ValueError):
        validate_request("videos/generations", body)


def test_aliases_preserved_and_validated_before_charge():
    body = {
        "model": "seedance-2.5",
        "prompt": "reference",
        "seconds": 5,
        "size": "1280x720",
        "input_references": [{"type": "video_url", "video_url": {"url": "https://example.com/video.mp4"}}],
    }
    assert validate_request("videos/generations", body) == body
    assert normalized_video(body)["resolution"] == "720p"
    assert video_reserve_seconds(body) == 35
    with pytest.raises(ValueError):
        validate_request("videos/generations", {**body, "duration": 8})
    with pytest.raises(ValueError):
        validate_request("videos/generations", {**body, "reference_videos": []})


@pytest.mark.parametrize(
    "field,value",
    [
        ("duration", True),
        ("duration", 3),
        ("n", 2),
        ("generate_audio", False),
        ("seed", 123),
        ("resolution", "8k"),
        ("omni_reference_task_type", "extend"),
    ],
)
def test_reject_provider_unsupported_controls(field, value):
    with pytest.raises(ValueError):
        validate_request("videos/generations", {"model": "seedance-2.5", "prompt": "hi", field: value})


def test_sse_crlf_split_and_multiline_data():
    decoder = SSEDecoder()
    wire = 'event: usage\r\ndata: {"usage":\r\ndata: {"input_tokens":1}}\r\n\r\n'
    events = []
    for byte in wire:
        events.extend(decoder.feed(byte))
    assert len(events) == 1 and event_data(events[0]) == {"usage": {"input_tokens": 1}}


def test_known_live_image_cost_mismatch_fails_closed():
    from fastapi import HTTPException

    from app.catalog.models import PartnerPrice
    from app.inference.service import quote

    prices = [
        PartnerPrice(
            mode="default",
            resolution=tier,
            billing_unit="generation",
            price_rub=Decimal("100"),
            provider_cost_usdt=Decimal(".015"),
        )
        for tier in ("1K", "2K")
    ]
    with pytest.raises(HTTPException) as failure:
        quote("images/edits", {"model": "grok-imagine-image-2.0", "prompt": "edit"}, prices)
    assert failure.value.status_code == 503
