import asyncio
import copy
import hashlib
import time
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.infrastructure.config import get_settings
from app.media import video_inputs as inputs

DATA = Path(__file__).with_name("fixtures").joinpath("measured-five-seconds.mp4").read_bytes()
SOURCE = "https://source.example/video.mp4"
COPY = "https://storage.example/server-private-copy.mp4"
WRITE = "https://storage.example/upload?signature=private-test-write-capability"
BODY = {"model": "seedance-2.5", "prompt": "Edit", "omni_reference_task_type": "edit",
        "reference_videos": [{"url": SOURCE}], "resolution": "720p"}


def install_transport(monkeypatch, *, verify_data=DATA, source_status=200, source_headers=None, ticket_changes=None):
    calls, uploads, tickets = [], [], []
    monkeypatch.setattr(get_settings(), "media_probe_socket", "/tmp/synthetic-checker.sock")
    monkeypatch.setattr(inputs, "_inspect", AsyncMock(return_value=SimpleNamespace(
        duration=Fraction(5), video_duration=Fraction(5), audio_duration=None,
        content_type="video/mp4", sha256=hashlib.sha256(DATA).hexdigest(),
    )))

    async def handler(request):
        calls.append((request.method, str(request.url), dict(request.headers)))
        assert "authorization" not in request.headers
        if request.method == "PUT":
            uploads.append(await request.aread())
            return httpx.Response(204)
        data = DATA if str(request.url) == SOURCE else verify_data
        headers = {"Content-Length": str(len(data))}
        if str(request.url) == SOURCE:
            headers.update(source_headers or {})
        return httpx.Response(source_status if str(request.url) == SOURCE else 200,
                              headers=headers, stream=httpx.ByteStream(data))

    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False)

    class Adapter:
        async def native_request(self, protocol, body, *, headers):
            assert protocol == "media/uploads"  # No paid generation POST.
            assert body["size_bytes"] == len(DATA)
            assert headers == {"Accept-Encoding": "identity"}
            tickets.append(dict(body))
            data = {"upload_url": WRITE, "media_url": COPY, "upload_expires_at": int(time.time()) + 900,
                    "expires_at": int(time.time()) + 604800}
            data.update(ticket_changes or {})
            return httpx.Response(201, json=data)

    async def get_adapter(*args, **kwargs):
        return Adapter()

    monkeypatch.setattr(inputs, "public_http_client", factory)
    monkeypatch.setattr(inputs, "_upload_adapter", get_adapter)
    return calls, uploads, tickets


async def test_real_timing_proof_stages_and_verifies_exact_bytes(monkeypatch):
    calls, uploads, tickets = install_transport(monkeypatch)
    result = await inputs.prepare_video_inputs(None, "partner", BODY)
    assert result is not None
    assert result["billable_seconds"] == 10
    assert result["body"]["reference_videos"] == [{"url": COPY}]
    assert BODY["reference_videos"] == [{"url": SOURCE}]
    assert result["assets"][0]["sha256"] == hashlib.sha256(DATA).hexdigest()
    assert result["assets"][0]["duration"] == "5"
    assert uploads == [DATA] and len(tickets) == 1
    assert [(method, url) for method, url, _ in calls] == [("GET", SOURCE), ("PUT", WRITE), ("GET", COPY)]
    assert WRITE not in repr(result)


@pytest.mark.parametrize("media", [
    {"reference_videos": [{"url": SOURCE, "extra": "preserve"}]},
    {"video_urls": [SOURCE]},
    {"input_references": [{"type": "video_url", "video_url": SOURCE}]},
    {"input_references": [{"type": "video_url", "video_url": {"url": SOURCE, "extra": "keep"}}]},
])
async def test_aliases_rewrite_only_video_url_leaves(monkeypatch, media):
    install_transport(monkeypatch)
    body = {**{key: value for key, value in BODY.items() if key != "reference_videos"}, **media}
    result = await inputs.prepare_video_inputs(None, "partner", body)
    assert result is not None
    assert repr(result["body"]) == repr(body).replace(SOURCE, COPY)


@pytest.mark.parametrize("kwargs", [
    {"verify_data": b"changed copy"},
    {"source_status": 302}, {"source_status": 206},
    {"source_headers": {"Content-Encoding": "gzip"}},
    {"source_headers": {"Content-Length": str(len(DATA) + 1)}},
    {"source_headers": {"Content-Length": str(101 * 1024 * 1024)}},
    {"ticket_changes": {"media_url": SOURCE}},
    {"ticket_changes": {"upload_url": "http://storage.example/upload"}},
    {"ticket_changes": {"upload_expires_at": 1}},
    {"ticket_changes": {"expires_at": 1}},
])
async def test_unsafe_or_unverified_media_never_gets_smaller_hold(monkeypatch, kwargs):
    install_transport(monkeypatch, **kwargs)
    assert await inputs.prepare_video_inputs(None, "partner", BODY) is None


async def test_unsupported_timing_does_not_request_upload_ticket(monkeypatch):
    calls, uploads, tickets = install_transport(monkeypatch)
    monkeypatch.setattr(inputs, "_inspect", AsyncMock(return_value=None))
    assert await inputs.prepare_video_inputs(None, "partner", BODY) is None
    assert not tickets and not uploads and len(calls) == 1


async def test_fractional_rounding_is_up_per_input_and_edit_output(monkeypatch):
    install_transport(monkeypatch)
    monkeypatch.setattr(inputs, "_inspect", AsyncMock(return_value=SimpleNamespace(
        duration=Fraction(501, 100), video_duration=Fraction(5), audio_duration=Fraction(501, 100),
        content_type="video/mp4", sha256=hashlib.sha256(DATA).hexdigest(),
    )))
    result = await inputs.prepare_video_inputs(None, "partner", BODY)
    assert result["billable_seconds"] == 12


async def test_reference_default_output_and_duplicate_source_are_not_double_downloaded(monkeypatch):
    calls, uploads, tickets = install_transport(monkeypatch)
    body = copy.deepcopy(BODY)
    body.pop("omni_reference_task_type")
    body["reference_videos"].append({"url": SOURCE})
    result = await inputs.prepare_video_inputs(None, "partner", body)
    assert result["billable_seconds"] == 15  # Default output 5 + two inputs of 5.
    assert len(result["assets"]) == 2
    assert len(calls) == 3 and len(uploads) == len(tickets) == 1


async def test_saturated_or_same_partner_preparation_falls_back_without_waiting(monkeypatch):
    monkeypatch.setattr(get_settings(), "media_probe_socket", "/tmp/synthetic-checker.sock")
    started, finish = asyncio.Queue(), asyncio.Event()

    async def hold(db, partner_id, original, video, edit):
        await started.put(partner_id)
        await finish.wait()
        return None

    monkeypatch.setattr(inputs, "_prepare", hold)
    first = asyncio.create_task(inputs.prepare_video_inputs(None, "a", BODY))
    assert await started.get() == "a"
    assert await inputs.prepare_video_inputs(None, "a", BODY) is None
    second = asyncio.create_task(inputs.prepare_video_inputs(None, "b", BODY))
    assert await started.get() == "b"
    assert await inputs.prepare_video_inputs(None, "c", BODY) is None
    assert started.empty()
    finish.set()
    await asyncio.gather(first, second)
    assert not inputs._active_partners[asyncio.get_running_loop()]


async def test_total_timeout_releases_resource_slot_without_optimistic_quote(monkeypatch):
    monkeypatch.setattr(get_settings(), "media_probe_socket", "/tmp/synthetic-checker.sock")
    async def hang(*args):
        await asyncio.sleep(10)

    monkeypatch.setattr(inputs, "_prepare", hang)
    monkeypatch.setattr(inputs, "PREPARATION_TIMEOUT_SECONDS", .01)
    assert await inputs.prepare_video_inputs(None, "partner", BODY) is None
    assert not inputs._active_partners[asyncio.get_running_loop()]


@pytest.mark.parametrize("model", ["seedance-2.0", "seedance-2.0-fast", "wan-3", "minimax-h3"])
async def test_other_models_keep_existing_quotes_and_fallback_lanes(monkeypatch, model):
    def forbidden(**kwargs):
        raise AssertionError("Unreviewed measurement must not fetch any bytes")

    monkeypatch.setattr(inputs, "public_http_client", forbidden)
    assert await inputs.prepare_video_inputs(None, "partner", {**BODY, "model": model}) is None


@pytest.mark.parametrize("url", [
    "https://public.example:bad/a", "https://public.example:0/a", "https://public.example:65536/a",
    "https://[invalid/a",
])
async def test_malformed_authority_keeps_conservative_quote_without_http(monkeypatch, url):
    calls, _, tickets = install_transport(monkeypatch)
    body = {**BODY, "reference_videos": [{"url": url}]}
    assert await inputs.prepare_video_inputs(None, "partner", body) is None
    assert not calls and not tickets


async def test_httpx_url_parser_failure_keeps_conservative_quote(monkeypatch):
    install_transport(monkeypatch)

    async def reject(*args, **kwargs):
        raise httpx.InvalidURL("synthetic invalid authority")

    monkeypatch.setattr(inputs, "_download", reject)
    assert await inputs.prepare_video_inputs(None, "partner", BODY) is None
