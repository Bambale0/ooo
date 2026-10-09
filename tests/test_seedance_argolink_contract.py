"""Reviewed provider contract: no live calls or customer media.

The detailed ArgoLink edit guide explicitly forbids extra assets, even though
its generic field table says 'reference and edit'. Do not turn a doc conflict
into an unverified acceptance promise or silently rewrite edit as reference.
"""

import copy

import pytest

from app.contracts.registry import validate_request, video_reserve_seconds

PROTOCOL = "videos/generations"


def edit_request(**controls):
    return {
        "model": "seedance-2.5",
        "prompt": "Make the colors warmer in @Video 1",
        "omni_reference_task_type": "edit",
        "resolution": "480p",
        "reference_videos": [{"url": "https://example.org/source.mp4"}],
        **controls,
    }


@pytest.mark.parametrize("controls", [{}, {"duration": -1, "aspect_ratio": "adaptive"}])
def test_single_video_edit_keeps_controls_and_conservative_accounting(controls):
    body = edit_request(**controls)
    original = copy.deepcopy(body)
    validated = validate_request(PROTOCOL, body)
    assert validated == original and body == original
    assert validated is not body
    assert video_reserve_seconds(validated) == 60


@pytest.mark.parametrize("wire_format", ["native", "url_aliases", "input_references"])
def test_apix_photo_video_edit_is_not_silently_converted_to_reference(wire_format):
    body = edit_request(reference_images=[{"url": "https://example.org/appearance.jpg"}])
    if wire_format == "url_aliases":
        for field, alias in (("reference_images", "image_urls"), ("reference_videos", "video_urls")):
            body[alias] = [item["url"] for item in body.pop(field)]
    elif wire_format == "input_references":
        body["input_references"] = []
        for field, kind in (("reference_images", "image_url"), ("reference_videos", "video_url")):
            body["input_references"].extend({"type": kind, kind: item} for item in body.pop(field))
    original = copy.deepcopy(body)
    with pytest.raises(ValueError, match="edit_requires_single_video"):
        validate_request(PROTOCOL, body)
    assert body == original


@pytest.mark.parametrize("mode", ["text", "reference", "edit"])
@pytest.mark.parametrize("audio", [True, False])
def test_seedance_25_preserves_documented_audio_boolean(mode, audio):
    body = edit_request(generate_audio=audio)
    if mode != "edit":
        body.pop("omni_reference_task_type")
        body["duration"] = 5
    if mode == "reference":
        body["reference_images"] = [{"url": "https://example.org/image.jpg"}]
    if mode == "text":
        body.pop("reference_videos")
    assert validate_request(PROTOCOL, body) == body


@pytest.mark.parametrize("audio", [None, 0, 1, "false", "true", [], {}])
def test_seedance_audio_control_rejects_non_booleans(audio):
    with pytest.raises(ValueError, match="invalid_generate_audio"):
        validate_request(PROTOCOL, {"model": "seedance-2.5", "prompt": "Animate", "generate_audio": audio})


def test_reference_asset_limits_and_accounting_are_unchanged():
    body = {
        "model": "seedance-2.5",
        "prompt": "Combine references",
        "duration": 5,
        "reference_images": [{"url": "https://example.org/image.jpg"}] * 30,
        "reference_videos": [{"url": "https://example.org/video.mp4"}] * 10,
        "reference_audios": [{"url": "https://example.org/audio.mp3"}] * 10,
    }
    assert validate_request(PROTOCOL, body) == body
    assert video_reserve_seconds(body) == 35
    for field in ("reference_images", "reference_videos", "reference_audios"):
        invalid = copy.deepcopy(body)
        invalid[field].append(invalid[field][0])
        with pytest.raises(ValueError, match="reference_limit_exceeded"):
            validate_request(PROTOCOL, invalid)


def test_other_provider_audio_contract_is_not_relaxed():
    with pytest.raises(ValueError, match="unsupported_generation_control"):
        validate_request(PROTOCOL, {"model": "minimax-h3", "prompt": "Animate", "generate_audio": False})
