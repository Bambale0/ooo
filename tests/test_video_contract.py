from dataclasses import replace

import pytest
from pydantic import ValidationError

from app.generations.schemas import GenerationCreate
from app.providers.base import ProviderGenerationRequest
from app.providers.video_contract import validate_video_request, video_request_body

BASE = ProviderGenerationRequest(
    generation_id="private-id",
    model_slug="seedance-2.5",
    mode="text_to_video",
    resolution="720p",
    prompt="A moving train",
    duration_seconds=5,
)


@pytest.mark.parametrize(
    "changes",
    [
        {"model_slug": "seedance-2.0", "duration_seconds": 16},
        {"duration_seconds": 3},
        {"duration_seconds": 31},
        {"model_slug": "seedance-2.0-fast", "resolution": "1080p"},
        {"resolution": "4k"},
        {"aspect_ratio": "adaptive"},
        {"mode": "reference", "reference_images": ("https://example.com/a.jpg",) * 31},
        {"mode": "image_to_video"},
        {"mode": "first_last_frame", "start_image": "https://example.com/a.jpg"},
        {"model_slug": "grok-imagine-video-1.5", "duration_seconds": 16},
        {
            "model_slug": "grok-imagine-video-1.5",
            "mode": "reference",
            "resolution": "1080p",
            "reference_images": ("https://example.com/a.jpg",),
        },
        {"model_slug": "unknown"},
        {"mode": "edit"},
        {"mode": "first_frame", "start_image": "https://example.com/a.jpg", "aspect_ratio": "16:9"},
        {"mode": "reference", "reference_images": ("https://user:secret@example.com/a.jpg",)},
    ],
)
def test_invalid_model_combinations_are_rejected(changes):
    with pytest.raises(ValueError):
        validate_video_request(replace(BASE, **changes))


@pytest.mark.parametrize("model", ["seedance-2.5", "seedance-2.5-self-developed-nsfw"])
def test_seedance_25_family_shares_its_limit_profile(model):
    """Both 2.5 variants accept 4-30s, 30 references and 1080p.

    Guards the family-based limits: an exact-slug comparison would silently give a
    new variant the 2.0 profile (15s, 9 references) and reject valid paid requests.
    """
    validate_video_request(replace(BASE, model_slug=model, duration_seconds=30, resolution="1080p"))
    validate_video_request(
        replace(
            BASE,
            model_slug=model,
            mode="reference",
            resolution="1080p",
            reference_images=("https://example.com/a.jpg",) * 30,
        )
    )
    # 4k and 31 seconds belong to the 2.0 family only, never to 2.5.
    with pytest.raises(ValueError, match="unsupported_resolution"):
        validate_video_request(replace(BASE, model_slug=model, resolution="4k"))
    with pytest.raises(ValueError, match="unsupported_duration"):
        validate_video_request(replace(BASE, model_slug=model, duration_seconds=31))
    # 480p is advertised for the NSFW variant but rejected by the provider, so the
    # reviewed contract drops that tier and the request must fail before any charge.
    if model == "seedance-2.5-self-developed-nsfw":
        with pytest.raises(ValueError, match="unsupported_resolution"):
            validate_video_request(replace(BASE, model_slug=model, resolution="480p"))
    # start_image restricts the aspect ratio to adaptive in the 2.5 family.
    validate_video_request(
        replace(BASE, model_slug=model, mode="first_frame", start_image="https://example.com/a.jpg")
    )
    with pytest.raises(ValueError, match="unsupported_aspect_ratio"):
        validate_video_request(
            replace(
                BASE,
                model_slug=model,
                mode="first_frame",
                start_image="https://example.com/a.jpg",
                aspect_ratio="16:9",
            )
        )


@pytest.mark.parametrize("model,first_field", [("seedance-2.5", "start_image"), ("grok-imagine-video-1.5", "image")])
def test_first_frame_uses_model_specific_wire_field(model, first_field):
    body = video_request_body(
        replace(
            BASE,
            model_slug=model,
            mode="first_frame",
            start_image="https://example.com/a.jpg",
        )
    )
    assert body == {
        "model": model,
        "prompt": BASE.prompt,
        "duration": 5,
        "resolution": "720p",
        first_field: {"url": "https://example.com/a.jpg"},
    }


def test_seedance_last_frame_is_forwarded():
    body = video_request_body(
        replace(
            BASE,
            mode="first_last_frame",
            start_image="https://example.com/a.jpg",
            end_image="https://example.com/b.jpg",
        )
    )
    assert body["end_image"] == {"url": "https://example.com/b.jpg"}


def test_normalized_seedance_frame_accepts_adaptive():
    body = video_request_body(
        replace(
            BASE,
            mode="first_frame",
            start_image="https://example.com/a.jpg",
            aspect_ratio="adaptive",
        )
    )
    assert body["aspect_ratio"] == "adaptive"


@pytest.mark.parametrize(
    "field,value",
    [
        ("reference_videos", [{"url": "https://example.com/a.mp4"}]),
        ("reference_audios", []),
        ("seed", 123),
        ("generate_audio", False),
    ],
)
def test_unimplemented_billable_or_semantic_fields_are_not_silently_dropped(field, value):
    with pytest.raises(ValidationError):
        GenerationCreate(model_slug="seedance-2.5", prompt="test", idempotency_key="request-1", **{field: value})
