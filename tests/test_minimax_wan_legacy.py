from dataclasses import replace

import pytest

from app.providers.base import ProviderGenerationRequest
from app.providers.video_contract import validate_video_request, video_request_body


def request(model, **fields):
    return ProviderGenerationRequest(
        generation_id="test", model_slug=model, mode="default",
        resolution="768p" if model == "minimax-h3" else "720p",
        prompt="A ceramic cup", duration_seconds=5, **fields,
    )


@pytest.mark.parametrize("model", ["minimax-h3", "wan-3", "wan-3-prime"])
def test_legacy_text_and_reference_inputs_share_native_contract(model):
    value = request(model, reference_images=("https://media.example.com/img.png",) * 9)
    validate_video_request(value)
    assert len(video_request_body(value)["reference_images"]) == 9


@pytest.mark.parametrize("model", ["minimax-h3", "wan-3"])
def test_legacy_frame_pair_preserves_correct_field_names(model):
    value = request(model, start_image="https://media.example.com/first.png",
                    end_image="https://media.example.com/last.png", aspect_ratio="adaptive")
    result = video_request_body(value)
    assert result["start_image"]["url"].endswith("first.png")
    assert result["end_image"]["url"].endswith("last.png")
    assert "image" not in result


@pytest.mark.parametrize("model", ["minimax-h3", "wan-3", "wan-3-prime"])
def test_legacy_does_not_silently_drop_unbillable_video_audio(model):
    for field in ["reference_videos", "reference_audios"]:
        with pytest.raises(ValueError):
            video_request_body(request(model, **{field: ("https://media.example.com/input",)}))


def test_prime_frame_route_and_invalid_legacy_modes_are_rejected():
    with pytest.raises(ValueError):
        video_request_body(request("wan-3-prime", start_image="https://media.example.com/image.png"))
    for mode in ["edit", "other", "first_frame", "first_last_frame"]:
        with pytest.raises(ValueError):
            video_request_body(replace(request("minimax-h3"), mode=mode))


def test_legacy_wan_supports_thirty_seconds():
    value = replace(request("wan-3"), duration_seconds=30)
    assert video_request_body(value)["duration"] == 30
