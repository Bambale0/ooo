"""Exercise the downloadable client script without any provider or production I/O."""

import json
import stat
from collections import deque
from types import SimpleNamespace

import httpx
import pytest

from app.api.examples import seedance_reference as example

API_BASE = "https://api.example.test"
API_KEY = "nrn_example_test_key"
UPLOAD_URL = "https://storage.example.test/upload?signed=example"
MEDIA_URL = "https://storage.example.test/reference?signed=example"
REQUEST_ID = "11111111-2222-4333-8444-555555555555"
CREATE_URL = API_BASE + "/v1/videos/generations"
STATUS_URL = API_BASE + "/v1/videos/" + REQUEST_ID
VIDEO_BYTES = b"example-video-content"


@pytest.fixture
def example_files(monkeypatch, tmp_path):
    source = tmp_path / "reference.PNG"
    source.write_bytes(b"example-png-content")
    state = tmp_path / "request.json"
    output = tmp_path / "result.mp4"
    for name, value in {
        "API_BASE": API_BASE + "/",
        "API_KEY": API_KEY,
        "REFERENCE_FILE": str(source),
        "STATE_FILE": str(state),
        "OUTPUT_FILE": str(output),
    }.items():
        monkeypatch.setenv(name, value)

    clock = SimpleNamespace(now=0, sleeps=[])

    def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(example, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    return SimpleNamespace(source=source, state=state, output=output, clock=clock)


@pytest.fixture
def scripted_http(monkeypatch):
    """Keep real httpx request/stream behavior while rejecting unplanned network calls."""
    original_client = httpx.Client
    plans = deque()
    requests = []

    def reject_network(self, request):
        raise AssertionError("The reference example must never contact real HTTP services in tests")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", reject_network)

    def handle(request):
        request.read()
        requests.append(request)
        assert plans, f"Unexpected request: {request.method} {request.url}"
        method, url, response = plans.popleft()
        assert (request.method, str(request.url)) == (method, url)
        if callable(response):
            response = response(request)
        return response

    def client(**kwargs):
        return original_client(transport=httpx.MockTransport(handle), trust_env=False, **kwargs)

    monkeypatch.setattr(example.httpx, "Client", client)
    yield SimpleNamespace(plans=plans, requests=requests)
    assert not plans, "The example did not complete the expected request sequence"


def prepare_submission(http, files, submission=None):
    def created(request):
        # The state must already survive a process restart at the moment POST leaves.
        saved = json.loads(files.state.read_text())
        assert saved["api_base"] == API_BASE
        assert saved["body"] == json.loads(request.content)
        assert saved["idempotency_key"] == request.headers["Idempotency-Key"]
        assert "request_id" not in saved
        return httpx.Response(202, json={"request_id": REQUEST_ID})

    http.plans.extend([
        ("POST", API_BASE + "/v1/media/uploads", httpx.Response(201, json={
            "upload_url": UPLOAD_URL,
            "media_url": MEDIA_URL,
            "upload_expires_at": "2030-01-01T00:15:00Z",
            "expires_at": "2030-01-08T00:00:00Z",
        })),
        ("PUT", UPLOAD_URL, httpx.Response(200)),
        ("POST", CREATE_URL, submission or created),
    ])


def prepare_result(http):
    http.plans.extend([
        ("GET", STATUS_URL, httpx.Response(200, json={
            "request_id": REQUEST_ID,
            "status": "done",
            # A returned external URL must never receive the partner's credentials.
            "video": {"url": "https://untrusted.example.test/video.mp4"},
        })),
        ("GET", STATUS_URL + "/content", httpx.Response(
            200, content=VIDEO_BYTES, headers={"Content-Type": "video/mp4"},
        )),
    ])


def test_reference_example_uploads_polls_and_downloads_without_leaking_key(example_files, scripted_http):
    prepare_submission(scripted_http, example_files)
    scripted_http.plans.append(("GET", STATUS_URL, httpx.Response(
        200, json={"request_id": REQUEST_ID, "status": "pending"},
    )))
    prepare_result(scripted_http)

    example.run()

    ticket, upload, submission, *_ = scripted_http.requests
    assert json.loads(ticket.content) == {
        "model": "seedance-2.5", "type": "image", "content_type": "image/png",
        "size_bytes": len(example_files.source.read_bytes()),
    }
    assert upload.content == example_files.source.read_bytes()
    assert upload.headers["Content-Type"] == "image/png"
    assert int(upload.headers["Content-Length"]) == len(upload.content)
    assert "Authorization" not in upload.headers
    assert "x-api-key" not in upload.headers
    assert all(
        request.headers.get("Authorization") == "Bearer " + API_KEY
        for request in scripted_http.requests if request.url.host == "api.example.test"
    )
    assert json.loads(submission.content)["reference_images"] == [{"url": MEDIA_URL}]
    assert example_files.clock.sleeps == [10]
    assert example_files.output.read_bytes() == VIDEO_BYTES
    assert not example_files.output.with_suffix(".mp4.part").exists()
    state_text = example_files.state.read_text()
    assert json.loads(state_text)["request_id"] == REQUEST_ID
    assert API_KEY not in state_text
    assert stat.S_IMODE(example_files.state.stat().st_mode) == 0o600


@pytest.mark.parametrize("timeout_type", [httpx.ReadTimeout, httpx.WriteTimeout])
def test_ambiguous_submit_resumes_with_saved_body_and_key_without_uploading_again(
    example_files, scripted_http, timeout_type,
):
    def lost_response(request):
        saved = json.loads(example_files.state.read_text())
        assert saved["body"] == json.loads(request.content)
        assert saved["idempotency_key"] == request.headers["Idempotency-Key"]
        raise timeout_type("Connection interrupted after submission", request=request)

    prepare_submission(scripted_http, example_files, lost_response)
    with pytest.raises(timeout_type):
        example.run()

    original_state = json.loads(example_files.state.read_text())
    assert "request_id" not in original_state
    assert [request.url.path for request in scripted_http.requests].count("/v1/videos/generations") == 1
    assert not example_files.output.exists()
    # The resumed run must not need a source file or create a fresh media URL.
    example_files.source.unlink()
    scripted_http.plans.append(("POST", CREATE_URL, httpx.Response(202, json={"request_id": REQUEST_ID})))
    prepare_result(scripted_http)

    example.run()

    submissions = [request for request in scripted_http.requests if str(request.url) == CREATE_URL]
    assert len(submissions) == 2
    assert submissions[0].content == submissions[1].content
    assert submissions[0].headers["Idempotency-Key"] == submissions[1].headers["Idempotency-Key"]
    assert json.loads(example_files.state.read_text()) == {**original_state, "request_id": REQUEST_ID}
    assert example_files.output.read_bytes() == VIDEO_BYTES


@pytest.mark.parametrize("status", ["failed", "expired"])
def test_terminal_result_never_submits_a_replacement_on_restart(example_files, scripted_http, status):
    prepare_submission(scripted_http, example_files)
    terminal = {"request_id": REQUEST_ID, "status": status, "error": {"type": "generation_failed"}}
    scripted_http.plans.append(("GET", STATUS_URL, httpx.Response(200, json=terminal)))

    with pytest.raises(RuntimeError, match="generation_failed"):
        example.run()

    saved_state = example_files.state.read_bytes()
    scripted_http.plans.append(("GET", STATUS_URL, httpx.Response(200, json=terminal)))
    example_files.source.unlink()

    with pytest.raises(RuntimeError, match="generation_failed"):
        example.run()

    assert len([request for request in scripted_http.requests if str(request.url) == CREATE_URL]) == 1
    assert example_files.state.read_bytes() == saved_state
    assert not example_files.output.exists()


def test_polling_deadline_preserves_request_and_next_run_only_resumes_polling(
    example_files, scripted_http, monkeypatch,
):
    prepare_submission(scripted_http, example_files)
    scripted_http.plans.append(("GET", STATUS_URL, httpx.Response(
        200, json={"request_id": REQUEST_ID, "status": "pending"},
    )))

    def elapse_deadline(seconds):
        example_files.clock.now += 1801

    monkeypatch.setattr(example.time, "sleep", elapse_deadline)
    with pytest.raises(TimeoutError, match="SAME STATE_FILE"):
        example.run()

    saved_state = example_files.state.read_bytes()
    assert json.loads(saved_state)["request_id"] == REQUEST_ID
    assert not example_files.output.exists()
    prepare_result(scripted_http)
    example_files.source.unlink()

    example.run()

    assert len([request for request in scripted_http.requests if str(request.url) == CREATE_URL]) == 1
    assert example_files.state.read_bytes() == saved_state
    assert example_files.output.read_bytes() == VIDEO_BYTES


def test_rejected_download_preserves_existing_output_and_does_not_resubmit(example_files, scripted_http):
    prepare_submission(scripted_http, example_files)
    scripted_http.plans.extend([
        ("GET", STATUS_URL, httpx.Response(200, json={"request_id": REQUEST_ID, "status": "done"})),
        ("GET", STATUS_URL + "/content", httpx.Response(410, json={"detail": "media_asset_expired"})),
    ])
    example_files.output.write_bytes(b"previous-video")

    with pytest.raises(httpx.HTTPStatusError) as exc:
        example.run()

    assert exc.value.response.status_code == 410
    assert example_files.output.read_bytes() == b"previous-video"
    assert not example_files.output.with_suffix(".mp4.part").exists()
    assert json.loads(example_files.state.read_text())["request_id"] == REQUEST_ID
    assert len([request for request in scripted_http.requests if str(request.url) == CREATE_URL]) == 1
