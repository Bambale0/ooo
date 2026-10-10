import copy
from decimal import Decimal

import pytest

from app.catalog.sync import variants
from app.contracts.registry import MODELS, TEXT_PROTOCOLS, normalized_video, validate_request, video_reserve_seconds
from app.inference.streams import SSEDecoder, event_data


def test_minimax_reference_modes_limits_defaults_and_financial_units():
    body = {
        "model": "minimax-h3",
        "prompt": "Follow @Image 1",
        "duration": 4,
        "reference_images": [{"url": "https://example.org/photo.jpg"}],
        "reference_videos": [{"url": "https://example.org/video.mp4"}],
        "reference_audios": [{"url": "https://example.org/audio.mp3"}],
        "aspect_ratio": "adaptive",
        "generate_audio": True,
    }
    assert validate_request("videos/generations", body) == body
    assert video_reserve_seconds(body) == 19  # never trust caller-declared input video seconds
    frame = {
        "model": "minimax-h3",
        "prompt": "Animate",
        "resolution": "2k",
        "duration": 15,
        "start_image": {"url": "https://example.org/first.jpg"},
    }
    assert validate_request("videos/generations", frame) == frame
    frame["end_image"] = {"url": "https://example.org/last.jpg"}
    assert validate_request("videos/generations", frame) == frame
    for invalid in (
        {**body, "duration": 3},
        {**body, "generate_audio": False},
        {**body, "reference_images": [], "reference_videos": []},
        {**body, "reference_images": body["reference_images"] * 10},
        {**frame, "aspect_ratio": "16:9"},
        {**frame, "prompt": ""},
        {**frame, "resolution": "720p"},
    ):
        with pytest.raises(ValueError):
            validate_request("videos/generations", invalid)
    text = {"model": "minimax-h3", "prompt": "Animate", "size": "1366x768"}
    assert normalized_video(text)["resolution"] == "768p"
    with pytest.raises(ValueError, match="explicit_resolution"):
        normalized_video({**text, "size": "2048x1536"})
    assert normalized_video({**text, "size": "2048x1536", "resolution": "2k"})["resolution"] == "2k"


@pytest.mark.parametrize("model", ["seedance-2.0", "seedance-2.0-fast", "seedance-2.0-mini"])
def test_new_reviewed_seedance_480p_tiers(model):
    body = {"model": model, "prompt": "Animate", "duration": 4, "resolution": "480p"}
    assert validate_request("videos/generations", body) == body
    assert any(r == "480p" and c > 0 for _, r, _, c in variants(MODELS[model]))


@pytest.mark.parametrize(
    "resolution,cost",
    [("720p", "0.11"), ("1080p", "0.28"), ("4k", "0.58")],
)
def test_seedance_20_nsfw_reviewed_tiers_validate_and_preserve_billable_references(resolution, cost):
    model = "seedance-2.0-self-developed-nsfw"
    assert model in MODELS
    body = {"model": model, "prompt": "Animate", "duration": 15, "resolution": resolution}
    assert validate_request("videos/generations", body) == body
    assert ("default", resolution, "second", Decimal(cost)) in list(variants(MODELS[model]))
    body["reference_videos"] = [{"url": "https://example.org/clip.mp4"}]
    assert validate_request("videos/generations", body) == body
    assert video_reserve_seconds(body) == 30


def test_seedance_20_nsfw_rejects_provider_rejected_480p_before_charge():
    model = "seedance-2.0-self-developed-nsfw"
    assert model in MODELS
    assert "480p" not in {resolution for _, resolution, _, _ in variants(MODELS[model])}
    with pytest.raises(ValueError, match="unsupported_resolution"):
        validate_request("videos/generations", {"model": model, "prompt": "Animate", "resolution": "480p"})


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
        body = {
            "model": model,
            "prompt": "Animate",
            "duration": 5,
            "resolution": "768p" if model == "minimax-h3" else "720p",
        }
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


@pytest.mark.parametrize("model", sorted(name for name in MODELS if name.startswith("seedance-")))
@pytest.mark.parametrize("last_frame", [False, True])
def test_seedance_frames_accept_explicit_adaptive_without_rewriting(model, last_frame):
    body = {
        "model": model,
        "start_image": {"url": "https://example.org/first.jpg"},
        "aspect_ratio": "adaptive",
    }
    if last_frame:
        body["end_image"] = {"url": "https://example.org/last.jpg"}
    assert validate_request("videos/generations", body) == body


@pytest.mark.parametrize("ratio", ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"])
def test_seedance_20_frames_accept_fixed_ratio_but_25_rejects_it(ratio):
    body = {"model": "seedance-2.0", "image_url": "https://example.org/first.jpg", "ratio": ratio}
    assert validate_request("videos/generations", body) == body
    with pytest.raises(ValueError):
        validate_request("videos/generations", {**body, "model": "seedance-2.5"})


@pytest.mark.parametrize("controls", [{}, {"duration": -1}, {"seconds": -1, "ratio": "adaptive"}])
def test_seedance_edit_inherited_dimensions_keep_conservative_reserve(controls):
    body = {
        "model": "seedance-2.5",
        "prompt": "Change the lighting",
        "omni_reference_task_type": "edit",
        "reference_videos": [{"url": "https://example.org/clip.mp4"}],
        **controls,
    }
    assert validate_request("videos/generations", body) == body
    assert video_reserve_seconds(body) == 60


@pytest.mark.parametrize(
    "controls",
    [
        {"duration": 4},
        {"duration": "-1"},
        {"duration": -1.0},
        {"aspect_ratio": "16:9"},
    ],
)
def test_seedance_edit_rejects_explicit_output_dimensions(controls):
    with pytest.raises(ValueError):
        validate_request(
            "videos/generations",
            {
                "model": "seedance-2.5",
                "prompt": "Edit",
                "omni_reference_task_type": "edit",
                "reference_videos": [{"url": "https://example.org/clip.mp4"}],
                **controls,
            },
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("duration", True),
        ("duration", 3),
        ("n", 2),
        ("generate_audio", "false"),
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
