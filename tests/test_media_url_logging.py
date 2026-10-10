import io
import json
import logging

import pytest
from test_video_input_staging import BODY, COPY, DATA, SOURCE, WRITE, install_transport

from app.infrastructure.logging import SecretRedactingFormatter
from app.media.video_inputs import prepare_video_inputs


@pytest.mark.parametrize("copy_bytes", [DATA, b"mismatched copy"], ids=["exact-copy", "copy-mismatch"])
async def test_signed_source_upload_and_readback_urls_never_reach_formatted_logs(monkeypatch, copy_bytes):
    install_transport(monkeypatch, verify_data=copy_bytes)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SecretRedactingFormatter("%(name)s %(message)s"))
    logger = logging.getLogger("httpx")
    old_level = logger.level
    logger.setLevel(logging.INFO)
    monkeypatch.setattr(logger, "disabled", False)
    logger.addHandler(handler)
    try:
        await prepare_video_inputs(None, "partner", BODY)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
    output = stream.getvalue()
    assert "HTTP Request" in output and "[URL_REDACTED]" in output
    for private in (SOURCE, COPY, WRITE, "private-test-write-capability", "storage.example", "source.example"):
        assert private not in output


def test_signed_urls_are_redacted_from_nested_extras_and_exception_traces():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SecretRedactingFormatter("%(message)s"))
    logger = logging.getLogger("test.media_url_redaction")
    logger.addHandler(handler)
    try:
        try:
            raise ValueError("Could not use https://storage.example/файл?cap=SYNTHETIC_WRITE_SECRET")
        except ValueError:
            logger.exception("Upload failed", extra={"details": {"read": [COPY + "?token=SYNTHETIC_READ_SECRET"]}})
    finally:
        logger.removeHandler(handler)
    output = stream.getvalue()
    assert "SYNTHETIC_WRITE_SECRET" not in output and "SYNTHETIC_READ_SECRET" not in output
    assert json.loads(output)["details"] == {"read": ["[URL_REDACTED]"]}
