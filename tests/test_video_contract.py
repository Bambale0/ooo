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
