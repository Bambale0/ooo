"""Seedream image mismatch diagnostics preserve strict validation and private-data safety."""

import base64
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from app.inference.diagnostics import NativeRequestTrace
from app.inference.images import ImageResultDimensionsMismatch, image_usage


def test_seedream_provider_output_mismatch_has_actual_and_expected_dimensions():
    content = io.BytesIO()
    Image.new("RGB", (1024, 1365)).save(content, format="JPEG")
    response = {"data": [{"b64_json": base64.b64encode(content.getvalue()).decode()}]}
    with pytest.raises(ImageResultDimensionsMismatch, match="image_result_invalid_dimensions") as error:
        image_usage(
            {"model": "seedream-5.0-pro", "resolution": "1k", "aspect_ratio": "3:4"},
            response,
        )
    assert error.value.actual == (1024, 1365)
    assert error.value.expected == (960, 1280)



def test_seedream_real_provider_848x1104_result_is_recoverable_after_verification():
    # Observed 2026-11-23 from ArgoLink, KSU Seedream Pro 1K 3:4:
    # a valid JPEG arrived, but exact-size billing validation rejected it.
    # A verified near-frame result must reach the existing UNBILLED response
    # delivery path; the normal exact-size image_usage() must remain strict.
    from app.inference.images import recoverable_seedream_dimensions

    body = {"model": "seedream-5.0-pro", "resolution": "1k", "aspect_ratio": "3:4"}
    source = io.BytesIO()
    Image.new("RGB", (848, 1104)).save(source, format="JPEG")
    response = {"data": [{"b64_json": base64.b64encode(source.getvalue()).decode()}]}
    with pytest.raises(ImageResultDimensionsMismatch) as mismatch:
        image_usage(body, response)
    assert mismatch.value.actual == (848, 1104)
    assert mismatch.value.expected == (960, 1280)
    assert recoverable_seedream_dimensions(body, mismatch.value)


@pytest.mark.parametrize("actual", [(800, 1104), (400, 520), (3500, 4600)])
def test_seedream_recovery_rejects_grossly_wrong_or_unbounded_rasters(actual):
    from app.inference.images import recoverable_seedream_dimensions

    mismatch = ImageResultDimensionsMismatch(actual, (960, 1280))
    assert not recoverable_seedream_dimensions(
        {"model": "seedream-5.0-pro", "resolution": "1k", "aspect_ratio": "3:4"},
        mismatch,
    )


def test_seedream_near_frame_recovery_does_not_apply_to_other_models():
    from app.inference.images import recoverable_seedream_dimensions

    assert not recoverable_seedream_dimensions(
        {"model": "nano-banana-pro", "resolution": "1k", "aspect_ratio": "3:4"},
        ImageResultDimensionsMismatch((848, 1104), (960, 1280)),
    )


def test_safe_mismatch_dimensions_are_logged_but_never_image_bytes_or_arbitrary_text(caplog):
    attempt = SimpleNamespace(raw_error=None)
    trace = NativeRequestTrace(context={"trace_id": "safe-test"})

    with caplog.at_level("WARNING"):
        trace.failed(
            attempt,
            ImageResultDimensionsMismatch((1024, 1365), (960, 1280)),
            phase="response_body_or_usage",
        )

    record = json.loads(attempt.raw_error)
    assert record["validation_reason"] == "image_result_invalid_dimensions"
    assert (
        record["actual_width"],
        record["actual_height"],
        record["expected_width"],
        record["expected_height"],
    ) == (1024, 1365, 960, 1280)
    assert caplog.records[-1].actual_width == 1024
    assert "b64_json" not in attempt.raw_error
    assert "1024x1365; PRIVATE" not in attempt.raw_error
