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
