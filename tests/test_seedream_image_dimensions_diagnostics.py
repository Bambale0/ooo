"""Seedream image mismatch diagnostics preserve strict validation and private-data safety."""

import base64
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from app.inference.diagnostics import NativeRequestTrace
from app.inference.images import (
    ImageResultDimensionsMismatch,
    ImageResultFormatMismatch,
    image_usage,
    recoverable_profile_image_dimensions,
    recoverable_profile_image_format,
)


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

    body = {"model": "seedream-5.0-pro", "resolution": "1k", "aspect_ratio": "3:4"}
    source = io.BytesIO()
    Image.new("RGB", (848, 1104)).save(source, format="JPEG")
    response = {"data": [{"b64_json": base64.b64encode(source.getvalue()).decode()}]}
    with pytest.raises(ImageResultDimensionsMismatch) as mismatch:
        image_usage(body, response)
    assert mismatch.value.actual == (848, 1104)
    assert mismatch.value.expected == (960, 1280)
    assert recoverable_profile_image_dimensions(body, mismatch.value)


@pytest.mark.parametrize(
    ("model", "encoding", "actual"),
    [
        ("seedream-5.0-pro", "JPEG", (1600, 600)),
        ("seedream-5.0-pro", "JPEG", (350, 2100)),
        ("seedream-5.5", "PNG", (1600, 600)),
        ("seedream-5.5", "PNG", (350, 2100)),
    ],
)
def test_fully_decoded_output_is_deliverable_at_any_aspect_ratio(model, encoding, actual):
    # Red for the historic 3% rule. The requested ratio is NOT a delivery gate.
    # Exact-size settlement remains forbidden by image_usage.
    body = {"model": model, "resolution": "1k", "aspect_ratio": "3:4"}
    source = io.BytesIO()
    Image.new("RGB", actual).save(source, format=encoding)
    data = {"data": [{"b64_json": base64.b64encode(source.getvalue()).decode()}]}
    with pytest.raises(ImageResultDimensionsMismatch) as mismatch:
        image_usage(body, data)
    assert mismatch.value.actual == actual
    assert recoverable_profile_image_dimensions(body, mismatch.value)


@pytest.mark.parametrize("actual", [(0, 1104), (848, 0), (50000, 50000)])
def test_profile_image_delivery_still_rejects_invalid_or_unbounded_rasters(actual):
    assert not recoverable_profile_image_dimensions(
        {"model": "seedream-5.0-pro", "resolution": "1k", "aspect_ratio": "3:4"},
        ImageResultDimensionsMismatch(actual, (960, 1280)),
    )



@pytest.mark.parametrize(
    ("model", "encoding"),
    [("seedream-5.0-pro", "PNG"), ("seedream-5.5", "JPEG"), ("seedream-5.0-pro", "WEBP")],
)
def test_valid_generated_image_with_different_encoding_is_still_delivered(model, encoding):
    # The supplier finished a valid PNG/JPEG/WebP, even if its declared
    # representation differs. Do not lose customer media due to the envelope.
    body = {"model": model, "resolution": "1k", "aspect_ratio": "3:4"}
    source = io.BytesIO()
    Image.new("RGB", (960, 1280)).save(source, format=encoding)
    data = {"data": [{"b64_json": base64.b64encode(source.getvalue()).decode()}]}
    with pytest.raises(ImageResultFormatMismatch) as mismatch:
        image_usage(body, data)
    assert mismatch.value.actual == encoding
    assert recoverable_profile_image_format(body, mismatch.value)


def test_invalid_image_format_remains_rejected_without_unbilled_delivery():
    body = {"model": "seedream-5.0-pro", "resolution": "1k", "aspect_ratio": "3:4"}
    source = io.BytesIO()
    Image.new("RGB", (960, 1280)).save(source, format="GIF")
    data = {"data": [{"b64_json": base64.b64encode(source.getvalue()).decode()}]}
    with pytest.raises(ValueError, match="image_result_invalid_format") as error:
        image_usage(body, data)
    assert not isinstance(error.value, ImageResultFormatMismatch)


def test_profile_image_recovery_does_not_apply_to_unprofiled_models():
    assert not recoverable_profile_image_dimensions(
        {"model": "nano-banana-pro", "resolution": "1k", "aspect_ratio": "3:4"},
        ImageResultDimensionsMismatch((848, 1104), (960, 1280)),
    )


def test_profile_image_recovery_does_not_apply_to_size_limited_models():
    assert not recoverable_profile_image_dimensions(
        {"model": "gpt-image-2.6-flare", "resolution": "1k", "aspect_ratio": "3:4"},
        ImageResultDimensionsMismatch((848, 1104), (960, 1280)),
    )



def test_format_mismatch_diagnostics_are_allowlisted_and_never_log_media(caplog):
    attempt = SimpleNamespace(raw_error=None)
    trace = NativeRequestTrace(context={"trace_id": "safe-format-test"})
    with caplog.at_level("WARNING"):
        trace.failed(attempt, ImageResultFormatMismatch("PNG", "JPEG"), phase="response_body_or_usage")
    event = json.loads(attempt.raw_error)
    assert event["validation_reason"] == "image_result_invalid_format"
    assert event["actual_format"] == "PNG"
    assert event["expected_format"] == "JPEG"
    assert "b64_json" not in caplog.text



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
