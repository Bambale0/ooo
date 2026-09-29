import pytest

from app.contracts.registry import validate_request


@pytest.mark.parametrize("resolution", ["1k", "2k", "4k"])
@pytest.mark.parametrize("ratio", ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "5:4", "4:5", "21:9"])
def test_nano_pro_accepts_all_documented_frames_and_four_outputs(resolution, ratio):
    body = {
        "model": "nano-banana-pro",
        "prompt": "A ceramic cup",
        "resolution": resolution,
        "aspect_ratio": ratio,
        "n": 4,
        "response_format": "b64_json",
    }
    assert validate_request("images/generations", body) == body


@pytest.mark.parametrize("reference_count", [1, 14])
def test_nano_pro_json_edits_preserve_references(reference_count):
    body = {
        "model": "nano-banana-pro",
        "prompt": "Combine these objects",
        "images": [{"image_url": f"https://example.org/reference-{i}.jpg"} for i in range(reference_count)],
        "resolution": "4k",
        "aspect_ratio": "21:9",
    }
    assert validate_request("images/edits", body) == body


@pytest.mark.parametrize(
    "controls",
    [
        {"images": [{"image_url": "https://example.org/reference.jpg"}] * 15},
        {"images": []},
        {"n": 5},
        {"n": 0},
        {"n": True},
        {"resolution": "8k"},
        {"aspect_ratio": "2:1"},
        {"response_format": "url"},
    ],
)
def test_nano_pro_rejects_unsupported_controls(controls):
    with pytest.raises(ValueError):
        validate_request("images/edits", {"model": "nano-banana-pro", "prompt": "Edit", **controls})


@pytest.mark.parametrize("model", ["nano-banana-2", "nano-banana-2-lite"])
@pytest.mark.parametrize(
    "controls",
    [
        {"resolution": "4k"},
        {"aspect_ratio": "21:9"},
        {"images": [{"image_url": "https://example.org/reference.jpg"}] * 4},
    ],
)
def test_nano_pro_expansion_does_not_change_other_nano_contracts(model, controls):
    with pytest.raises(ValueError):
        validate_request("images/edits", {"model": model, "prompt": "Edit", **controls})


@pytest.mark.parametrize("size", ["1024x1024", "1440x1920", "3840x2160"])
def test_sunburst_keeps_documented_sizes_and_maximum_batch_and_references(size):
    body = {
        "model": "gpt-image-2.5-sunburst",
        "prompt": "Combine these objects",
        "images": [{"image_url": f"https://example.org/reference-{i}.jpg"} for i in range(16)],
        "size": size,
        "n": 7,
        "quality": "high",
        "response_format": "b64_json",
    }
    assert validate_request("images/edits", body) == body


@pytest.mark.parametrize("controls", [{"n": 8}, {"images": [{"image_url": "https://example.org/x.jpg"}] * 17}])
def test_sunburst_rejects_more_than_seven_results_or_sixteen_references(controls):
    with pytest.raises(ValueError):
        validate_request("images/edits", {"model": "gpt-image-2.5-sunburst", "prompt": "Edit", **controls})
