import copy
from decimal import Decimal

import pytest

from app.contracts.registry import MODELS, normalized_video, validate_request, video_reserve_seconds

PROTOCOL = "videos/generations"
MEDIA = {"url": "https://media.example.org/reference.mp4"}


def valid(model, **controls):
    return {"model": model, "prompt": "A ceramic cup on a table", **controls}


def test_reviewed_prices_match_targeted_provider_catalog():
    expected = {
        "minimax-h3": {"768p": Decimal(".039"), "2k": Decimal(".064")},
        "wan-3": {"480p": Decimal(".038"), "720p": Decimal(".075"), "1080p": Decimal(".15")},
        "wan-3-prime": {"480p": Decimal(".051"), "720p": Decimal(".11"), "1080p": Decimal(".21")},
    }
    for slug, tiers in expected.items():
        entry = MODELS.get(slug)
        assert entry is not None, f"Missing model contract: {slug}"
        assert entry["category"] == "video"
        assert entry["endpoint"] == "/v1/videos/generations"
        assert {t["label"]: t["price"] for t in entry["procurement"]["tiers"]} == tiers


@pytest.mark.parametrize("model,minimum,maximum", [("minimax-h3", 4, 15), ("wan-3", 2, 30), ("wan-3-prime", 4, 30)])
def test_duration_bounds_defaults_and_wire_preservation(model, minimum, maximum):
    for duration in (minimum, maximum):
        body = valid(model, duration=duration)
        before = copy.deepcopy(body)
        assert validate_request(PROTOCOL, body) == before
        assert body == before
    assert validate_request(PROTOCOL, valid(model)) == valid(model)
    for duration in (minimum - 1, maximum + 1, True, 5.5, "5"):
        with pytest.raises(ValueError):
            validate_request(PROTOCOL, valid(model, duration=duration))


@pytest.mark.parametrize(
    "model,images,videos,audios,total",
    [("minimax-h3", 9, 3, 3, 12), ("wan-3", 10, 5, 5, 20), ("wan-3-prime", 10, 5, 5, 20)],
)
def test_reference_limits_and_bad_urls(model, images, videos, audios, total):
    body = valid(model, reference_images=[MEDIA] * images, reference_videos=[MEDIA] * videos)
    assert validate_request(PROTOCOL, body) == body
    for field, limit in (("reference_images", images), ("reference_videos", videos), ("reference_audios", audios)):
        with pytest.raises(ValueError, match="reference_limit"):
            validate_request(PROTOCOL, valid(model, **{field: [MEDIA] * (limit + 1)}))
    if images + videos + audios > total:
        with pytest.raises(ValueError, match="reference_limit"):
            validate_request(PROTOCOL, {**body, "reference_audios": [MEDIA] * audios})
    for value in ("https://media.example.org", {"url": "http://example.org/a"}, {"url": "https://a:b@example.org/a"}):
        with pytest.raises(ValueError, match="invalid_media_reference"):
            validate_request(PROTOCOL, valid(model, reference_videos=[value]))


@pytest.mark.parametrize("model,limit", [("minimax-h3", 7000), ("wan-3", 20000), ("wan-3-prime", 20000)])
def test_required_prompt_character_limits(model, limit):
    assert validate_request(PROTOCOL, valid(model, prompt="x" * limit))
    for prompt in ("", " ", "x" * (limit + 1)):
        with pytest.raises(ValueError, match="prompt"):
            validate_request(PROTOCOL, valid(model, prompt=prompt, reference_images=[MEDIA]))


@pytest.mark.parametrize("frames", [False, True])
def test_minimax_accepts_adaptive_frames_and_rejects_fixed_ratio(frames):
    body = valid("minimax-h3", start_image=MEDIA, aspect_ratio="adaptive")
    if frames:
        body["end_image"] = MEDIA
    assert validate_request(PROTOCOL, body) == body
    with pytest.raises(ValueError):
        validate_request(PROTOCOL, {**body, "aspect_ratio": "16:9"})


def test_wan_edit_preserves_omitted_dimensions_and_uses_30_second_ceiling():
    body = valid("wan-3", omni_reference_task_type="edit", reference_videos=[MEDIA], reference_images=[MEDIA])
    assert validate_request(PROTOCOL, body) == body
    assert video_reserve_seconds(body) == 30
    for extra in ({"duration": 5}, {"seconds": 5}, {"ratio": "16:9"}, {"size": "1280x720"}, {"start_image": MEDIA}):
        with pytest.raises(ValueError):
            validate_request(PROTOCOL, {**body, **extra})
    with pytest.raises(ValueError):
        validate_request(PROTOCOL, valid("wan-3", omni_reference_task_type="edit"))


@pytest.mark.parametrize("model", ["wan-3", "wan-3-prime"])
@pytest.mark.parametrize("duration,expected", [(5, 20), (20, 30), (29, 30)])
def test_wan_reference_billable_seconds_cap_and_aliases(model, duration, expected):
    body = valid(model, seconds=duration, video_urls=[MEDIA["url"]])
    assert validate_request(PROTOCOL, body) == body
    assert normalized_video(body)["reference_videos"] == [MEDIA]
    assert video_reserve_seconds(body) == expected
    assert video_reserve_seconds(valid(model, duration=duration)) == duration


def test_wan_and_prime_controls_are_distinct():
    standard = valid("wan-3", seed=4294967295, generate_audio=False, aspect_ratio="21:9")
    assert validate_request(PROTOCOL, standard) == standard
    audio_only = valid("wan-3", reference_audios=[MEDIA], aspect_ratio="adaptive")
    assert validate_request(PROTOCOL, audio_only) == audio_only
    for controls in ({"start_image": MEDIA}, {"end_image": MEDIA}, {"seed": 1}, {"generate_audio": False},
                     {"omni_reference_task_type": "edit", "reference_videos": [MEDIA]},
                     {"aspect_ratio": "adaptive"}, {"aspect_ratio": "21:9"}, {"reference_audios": [MEDIA]}):
        with pytest.raises(ValueError):
            validate_request(PROTOCOL, valid("wan-3-prime", **controls))
    for seed in (-1, 4294967296, True, "1"):
        with pytest.raises(ValueError, match="seed"):
            validate_request(PROTOCOL, valid("wan-3", seed=seed))


@pytest.mark.parametrize("model", ["minimax-h3", "wan-3", "wan-3-prime"])
def test_no_batch_videos_and_watermark(model):
    for controls in ({"n": 2}, {"watermark": True}, {"omni_reference_task_type": "unsupported"}):
        with pytest.raises(ValueError):
            validate_request(PROTOCOL, valid(model, **controls))


def test_existing_seedance_edit_keeps_scope_and_reserve():
    body = valid("seedance-2.5", omni_reference_task_type="edit", reference_videos=[MEDIA])
    assert validate_request(PROTOCOL, body) == body
    assert video_reserve_seconds(body) == 60
