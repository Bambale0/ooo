import httpx
import pytest

from ops.load import provider_stub
from ops.load.worker_seed import ACK, _assert_safe_worker_environment


@pytest.fixture(autouse=True)
async def reset_stub(monkeypatch):
    monkeypatch.setenv("LOAD_STUB_KEY_PREFIX", "local-load-partner-")
    monkeypatch.setenv("LOAD_STUB_PROCESSING_POLLS", "1")
    monkeypatch.setenv("LOAD_STUB_SUBMIT_429_EVERY", "0")
    monkeypatch.setenv("LOAD_STUB_POLL_429_EVERY", "0")
    monkeypatch.setenv("LOAD_STUB_SUBMIT_500_EVERY", "0")
    monkeypatch.setenv("LOAD_STUB_POLL_500_EVERY", "0")
    transport = httpx.ASGITransport(app=provider_stub.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://stub") as client:
        response = await client.post("/__load__/reset", headers={"X-Load-Test-Ack": "I_UNDERSTAND"})
        assert response.status_code == 200
    yield


async def test_provider_stub_requires_partner_key_and_completes_after_configured_polls():
    transport = httpx.ASGITransport(app=provider_stub.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://stub") as client:
        unauthorized = await client.post(
            "/v1/videos/generations",
            json={"model": "seedance-2.5", "prompt": "test", "duration": 5},
        )
        assert unauthorized.status_code == 401

        headers = {"Authorization": "Bearer local-load-partner-1"}
        submit = await client.post(
            "/v1/videos/generations",
            headers=headers,
            json={"model": "seedance-2.5", "prompt": "test", "duration": 5},
        )
        assert submit.status_code == 202
        request_id = submit.json()["request_id"]

        first_poll = await client.get(f"/v1/videos/{request_id}", headers=headers)
        second_poll = await client.get(f"/v1/videos/{request_id}", headers=headers)
        assert first_poll.json()["status"] == "pending"
        assert second_poll.json()["status"] == "done"
        assert second_poll.json()["usage"]["billed_seconds"] == 5

        content = await client.get(f"/v1/videos/{request_id}/content", headers=headers)
        assert content.status_code == 200
        assert content.headers["content-type"].startswith("video/mp4")


async def test_provider_stub_can_inject_retry_after(monkeypatch):
    monkeypatch.setenv("LOAD_STUB_SUBMIT_429_EVERY", "1")
    monkeypatch.setenv("LOAD_STUB_RETRY_AFTER_SECONDS", "2.5")
    transport = httpx.ASGITransport(app=provider_stub.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://stub") as client:
        response = await client.post(
            "/v1/videos/generations",
            headers={"Authorization": "Bearer local-load-partner-1"},
            json={"model": "seedance-2.5", "prompt": "test", "duration": 5},
        )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "2.5"


def test_worker_fixture_requires_loopback_provider_and_explicit_ack(monkeypatch):
    settings = __import__("app.infrastructure.config", fromlist=["get_settings"]).get_settings()
    original_env = settings.app_env
    original_url = settings.argolink_base_url
    try:
        settings.app_env = "test"
        settings.argolink_base_url = "https://argolink.io"
        monkeypatch.setenv("LOAD_WORKER_TEST_ACK", ACK)
        with pytest.raises(RuntimeError, match="loopback"):
            _assert_safe_worker_environment()

        settings.argolink_base_url = "http://127.0.0.1:18080"
        monkeypatch.delenv("LOAD_WORKER_TEST_ACK", raising=False)
        with pytest.raises(RuntimeError, match="LOAD_WORKER_TEST_ACK"):
            _assert_safe_worker_environment()
    finally:
        settings.app_env = original_env
        settings.argolink_base_url = original_url
