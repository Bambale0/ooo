"""Regression coverage for safe image-response failure diagnostics."""
import json
from types import SimpleNamespace

import pytest

from app.inference.diagnostics import NativeRequestTrace


@pytest.mark.parametrize("reason", [
    "image_result_invalid_dimensions",
    "image_result_invalid_format",
    "image_result_format_mismatch",
    "invalid_image_count",
    "image_result_missing_bytes",
    "image_result_too_large",
    "response_too_large",
])
def test_image_validation_reason_survives_failure(reason, caplog):
    attempt = SimpleNamespace(raw_error=None)
    trace = NativeRequestTrace(context={"trace_id": "test"})
    with caplog.at_level("WARNING"):
        trace.failed(attempt, ValueError(reason), phase="response_body_or_usage")
    event = json.loads(attempt.raw_error)
    assert event["validation_reason"] == reason
    assert caplog.records[-1].validation_reason == reason


@pytest.mark.parametrize("error", [
    ValueError("https://example.com/image?token=SECRET"),
    ValueError("image_result_invalid_dimensions token=SECRET"),
    ValueError({"token": "SECRET"}),
    json.JSONDecodeError("SECRET", "SECRET", 0),
])
def test_arbitrary_exception_content_is_never_recorded(error, caplog):
    attempt = SimpleNamespace(raw_error=None)
    with caplog.at_level("WARNING"):
        NativeRequestTrace(context={}).failed(attempt, error, phase="response_body_or_usage")
    assert "validation_reason" not in json.loads(attempt.raw_error)
    assert "SECRET" not in attempt.raw_error
    assert "SECRET" not in caplog.text
