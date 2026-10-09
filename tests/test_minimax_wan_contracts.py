from decimal import Decimal

import pytest

from app.contracts.registry import MODELS, normalized_video, validate_request, video_reserve_seconds

PROTOCOL = "videos/generations"
REF = {"url": "https://media.example.com/reference.mp4"}
IMAGE = {"url": "https://media.example.com/reference.png"}
MODELS_UNDER_TEST = ("minimax-h3", "wan-3", "wan-3-prime")


def validate(model, **controls):
    body = {"model": model, "prompt": "A ceramic cup on a studio table", **controls}
    result = validate_request(PROTOCOL, body)
    assert result == body
    assert result is not body
    return body


@pytest.mark.parametrize("model,minimum,maximum", [("minimax-h3", 4, 15), ("wan-3", 2, 30), ("wan-3-prime", 4, 30)])
def test_documented_duration_bounds_and_no_request_rewrite(model, minimum, maximum):
    validate(model)
    validate(model, duration=minimum)
    validate(model, seconds=maximum)
    for invalid in (minimum - 1, maximum + 1, True, 5.5, "5"):
        with pytest.raises(ValueError):
            validate(model, duration=invalid)


@pytest.mark.parametrize("model", MODELS_UNDER_TEST)
@pytest.mark.parametrize("n", [0, 2, True, "1"])
def test_single_video_only(model, n):
    with pytest.raises(ValueError):
        validate(model, n=n)


@pytest.mark.parametrize("ratio", ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16", "adaptive"])
def test_wan_standard_ratios_and_seed(ratio):
    validate("wan-3", aspect_ratio=ratio, generate_audio=False, seed=4294967295)


@pytest.mark.parametrize("seed", [-1, 4294967296, True, 1.1])
def test_wan_seed_invalid(seed):
    with pytest.raises(ValueError):
        validate("wan-3", seed=seed)


@pytest.mark.parametrize("ratio", ["16:9", "9:16", "1:1"])
def test_prime_supported_ratios(ratio):
    validate("wan-3-prime", aspect_ratio=ratio)


@pytest.mark.parametrize("controls", [
    {"aspect_ratio": "adaptive"}, {"aspect_ratio": "21:9"}, {"seed": 1},
    {"generate_audio": False}, {"start_image": IMAGE}, {"image_url": IMAGE["url"]},
    {"frame_images": [{"frame_type": "first_frame", **IMAGE}]},
    {"omni_reference_task_type": "edit", "reference_videos": [REF]},
])
def test_prime_rejects_unsupported_modes_and_controls(controls):
    with pytest.raises(ValueError):
        validate("wan-3-prime", **controls)


def test_minimax_frame_ratio_and_twelve_reference_limit():
    validate("minimax-h3", start_image=IMAGE, end_image=IMAGE, aspect_ratio="adaptive")
    validate("minimax-h3", reference_images=[IMAGE] * 9, reference_videos=[REF] * 3)
    with pytest.raises(ValueError, match="reference_limit_exceeded"):
        validate("minimax-h3", reference_images=[IMAGE] * 9, reference_videos=[REF] * 3,
                 reference_audios=[{"url": "https://media.example.com/audio.mp3"}])


@pytest.mark.parametrize("model,maximum", [("minimax-h3", 7000), ("wan-3", 20000), ("wan-3-prime", 20000)])
def test_prompt_character_limits(model, maximum):
    validate(model, prompt="x" * maximum)
    with pytest.raises(ValueError, match="prompt_too_long"):
        validate(model, prompt="x" * (maximum + 1))


def test_wan_audio_alone_but_not_minimax_or_prime():
    audio = {"url": "https://media.example.com/audio.mp3"}
    validate("wan-3", reference_audios=[audio])
    for model in ("minimax-h3", "wan-3-prime"):
        with pytest.raises(ValueError, match="audio_requires_visual_reference"):
            validate(model, reference_audios=[audio])


def test_wan_edit_keeps_derived_controls_absent_and_reserves_thirty_seconds():
    body = validate("wan-3", omni_reference_task_type="edit", reference_videos=[REF],
                    reference_images=[IMAGE], resolution="1080p")
    assert "duration" not in body and "aspect_ratio" not in body
    assert video_reserve_seconds(body) == 30
    for extra in ({"duration": 5}, {"seconds": 5}, {"aspect_ratio": "adaptive"},
                  {"ratio": "adaptive"}, {"size": "1920x1080"}, {"start_image": IMAGE}):
        with pytest.raises(ValueError):
            validate("wan-3", omni_reference_task_type="edit", reference_videos=[REF], **extra)


@pytest.mark.parametrize("model", MODELS_UNDER_TEST)
def test_video_ref_alias_reserve_is_conservative_without_changing_wire_body(model):
    body = validate(model, duration=10, video_urls=[REF["url"]])
    assert normalized_video(body)["reference_videos"] == [REF]
    assert video_reserve_seconds(body) == 25
    if model.startswith("wan-"):
        assert video_reserve_seconds(validate(model, duration=25, reference_videos=[REF])) == 30
    # Images never increase seconds or introduce an extra image charge.
    assert video_reserve_seconds(validate(model, duration=10, reference_images=[IMAGE] * 9)) == 10


@pytest.mark.parametrize("model", MODELS_UNDER_TEST)
@pytest.mark.parametrize("value", [{"url": "http://media.example/a.mp4"}, {"url": "https://u:p@media.example/a"}, "bad"])
def test_https_media_objects_required(model, value):
    with pytest.raises(ValueError):
        validate(model, reference_videos=[value])


@pytest.mark.parametrize("model", MODELS_UNDER_TEST)
@pytest.mark.parametrize("controls", [{"watermark": True}, {"generate_audio": "false"}, {"watermark": 1}])
def test_bad_controls_fail_before_reserve(model, controls):
    with pytest.raises(ValueError):
        validate(model, **controls)


def test_minimax_size_maps_to_768p():
    body = validate("minimax-h3", size="1366x768")
    assert normalized_video(body)["resolution"] == "768p"


def test_catalog_targets_have_current_reviewed_procurement():
    expected = {
        "minimax-h3": {"768p": Decimal(".039"), "2k": Decimal(".064")},
        "wan-3": {"480p": Decimal(".038"), "720p": Decimal(".075"), "1080p": Decimal(".15")},
        "wan-3-prime": {"480p": Decimal(".051"), "720p": Decimal(".11"), "1080p": Decimal(".21")},
    }
    for model, rates in expected.items():
        assert {tier["label"]: Decimal(str(tier["price"]))
                for tier in MODELS[model]["procurement"]["tiers"]} == rates
