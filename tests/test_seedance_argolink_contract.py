"""Seedance 2.5 gateway regressions; no live provider calls or customer media.

The ArgoLink model card lists reference arrays for both reference and edit.
Its integration guide contradicts that card. Preserve bounded, economically
reserved native requests; do not silently change edit into reference or delete
assets. The provider remains authoritative for semantic acceptance.
"""

import copy

import pytest

from app.contracts.registry import validate_request, video_reserve_seconds

PROTOCOL = "videos/generations"


def edit_request(**controls):
    return {
        "model": "seedance-2.5",
        "prompt": "Use the appearance from @Image 1 in @Video 1",
        "omni_reference_task_type": "edit",
        "resolution": "480p",
        "reference_images": [{"url": "https://example.org/appearance.jpg"}],
        "reference_videos": [{"url": "https://example.org/source.mp4"}],
        **controls,
    }


@pytest.mark.parametrize("controls", [{}, {"duration": -1, "aspect_ratio": "adaptive"}, {"seconds": -1, "ratio": "adaptive"}])
def test_apix_edit_with_photo_reaches_native_contract_without_semantic_rewrite(controls):
    body = edit_request(**controls)
    original = copy.deepcopy(body)
    validated = validate_request(PROTOCOL, body)
    assert validated == original
    assert body == original
    assert validated is not body
    assert validated["reference_images"] is not body["reference_images"]
    assert video_reserve_seconds(validated) == 60


@pytest.mark.parametrize("wire_format", ["native", "url_aliases", "input_references"])
def test_edit_keeps_all_media_types_and_conservative_accounting(wire_format):
    body = edit_request(reference_audios=[{"url": "https://example.org/sound.mp3"}])
    if wire_format == "url_aliases":
        for field, alias in (("reference_images", "image_urls"), ("reference_videos", "video_urls"), ("reference_audios", "audio_urls")):
            body[alias] = [item["url"] for item in body.pop(field)]
    elif wire_format == "input_references":
        body["input_references"] = []
        for field, kind in (("reference_images", "image_url"), ("reference_videos", "video_url"), ("reference_audios", "audio_url")):
            body["input_references"].extend({"type": kind, kind: item} for item in body.pop(field))
    original = copy.deepcopy(body)
    assert validate_request(PROTOCOL, body) == original
    assert body == original
    assert video_reserve_seconds(body) == 60


def test_edit_reference_count_limits_still_apply():
    body = edit_request(
        reference_images=[{"url": "https://example.org/image.jpg"}] * 30,
        reference_videos=[{"url": "https://example.org/video.mp4"}] * 10,
        reference_audios=[{"url": "https://example.org/audio.mp3"}] * 10,
    )
    assert validate_request(PROTOCOL, body) == body
    assert video_reserve_seconds(body) == 60
    for field in ("reference_images", "reference_videos", "reference_audios"):
        invalid = copy.deepcopy(body)
        invalid[field].append(invalid[field][0])
        with pytest.raises(ValueError, match="reference_limit_exceeded"):
            validate_request(PROTOCOL, invalid)


@pytest.mark.parametrize("mode", ["text", "reference", "edit"])
@pytest.mark.parametrize("audio", [True, False])
def test_seedance_25_preserves_explicit_audio_boolean(mode, audio):
    body = edit_request(generate_audio=audio)
    if mode != "edit":
        body.pop("omni_reference_task_type")
        body["duration"] = 5
    if mode == "text":
        body.pop("reference_images")
        body.pop("reference_videos")
    assert validate_request(PROTOCOL, body) == body


@pytest.mark.parametrize("audio", [None, 0, 1, "false", "true", [], {}])
def test_seedance_audio_control_rejects_non_booleans(audio):
    with pytest.raises(ValueError, match="invalid_generate_audio"):
        validate_request(PROTOCOL, {"model": "seedance-2.5", "prompt": "Animate", "generate_audio": audio})


@pytest.mark.parametrize(
    "controls",
    [
        {"reference_videos": []},
        {"duration": 4},
        {"aspect_ratio": "16:9"},
        {"size": "1280x720"},
        {"start_image": {"url": "https://example.org/frame.jpg"}},
        {"seed": 123},
        {"watermark": True},
        {"reference_images": [{"url": "http://example.org/image.jpg"}]},
        {"reference_images": [{"url": "https://user:password@example.org/image.jpg"}]},
        {"reference_images": ["https://example.org/image.jpg"]},
    ],
)
def test_edit_still_rejects_unsafe_or_unaccountable_requests(controls):
    with pytest.raises(ValueError):
        validate_request(PROTOCOL, edit_request(**controls))


def test_other_provider_audio_contract_is_not_relaxed():
    with pytest.raises(ValueError, match="unsupported_generation_control"):
        validate_request(PROTOCOL, {"model": "minimax-h3", "prompt": "Animate", "generate_audio": False})
