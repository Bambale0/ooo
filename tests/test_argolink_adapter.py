import httpx
import pytest

from app.providers.argolink import ArgoLinkAdapter
from app.providers.base import ProviderAdapterError, ProviderGenerationRequest
from app.providers.http_client import close_provider_http_clients, get_provider_http_client


async def test_argolink_adapter_validates_key_against_authenticated_video_status_endpoint():
    seen_authorization: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/videos/00000000-0000-0000-0000-000000000000":
            seen_authorization.append(request.headers.get("Authorization"))
            if request.headers.get("Authorization") == "Bearer valid-key":
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(401, json={"error": "unauthorized"})
        return httpx.Response(404, json={"error": "not found"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(base_url="https://argolink.io", transport=transport) as client:
        adapter = ArgoLinkAdapter(base_url="https://argolink.io", api_key=None, client=client)
        assert await adapter.validate_key("valid-key") is True
        assert await adapter.validate_key("bad-key") is False

    assert seen_authorization == ["Bearer valid-key", "Bearer bad-key"]


async def test_argolink_adapter_submits_polls_and_streams_with_configured_key():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers.get("Authorization") == "Bearer test-key"
        if request.method == "POST" and request.url.path == "/v1/videos/generations":
            body = request.read().decode("utf-8")
            assert "seedance-2.5" in body
            assert "generation-1" in body
            assert "\"duration\":5" in body
            assert "\"aspect_ratio\":\"9:16\"" in body
            assert "https://cdn.example.test/reference.jpg" in body
            return httpx.Response(202, json={"request_id": "video_task_123"})
        if request.method == "GET" and request.url.path == "/v1/videos/video_task_123":
            return httpx.Response(200, json={"status": "done"})
        if request.method == "GET" and request.url.path == "/v1/videos/video_task_123/content":
            assert request.headers.get("Range") == "bytes=0-3"
            return httpx.Response(
                206,
                content=b"vide",
                headers={
                    "content-type": "video/mp4",
                    "content-length": "4",
                    "content-range": "bytes 0-3/11",
                    "accept-ranges": "bytes",
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        base_url="https://argolink.io",
        transport=transport,
        headers={"Content-Type": "application/json"},
    ) as client:
        adapter = ArgoLinkAdapter(
            base_url="https://argolink.io",
            api_key="test-key",
            client=client,
        )

        result = await adapter.submit_generation(
            ProviderGenerationRequest(
                generation_id="generation-1",
                model_slug="seedance-2.5",
                mode="text_to_video",
                resolution="720p",
                prompt="launch video",
                duration_seconds=5,
                aspect_ratio="9:16",
                reference_images=("https://cdn.example.test/reference.jpg",),
            )
        )
        poll_result = await adapter.poll_generation(result.provider_task_id)
        stream = await adapter.open_result_stream(
            poll_result.result_url or "",
            range_header="bytes=0-3",
        )
        content = b"".join([chunk async for chunk in stream.body])

    assert result.provider_task_id == "video_task_123"
    assert poll_result.status == "completed"
    assert poll_result.result_url == "https://argolink.io/v1/videos/video_task_123/content"
    assert stream.status_code == 206
    assert stream.content_type == "video/mp4"
    assert stream.content_length == 4
    assert stream.content_range == "bytes 0-3/11"
    assert stream.accept_ranges == "bytes"
    assert content == b"vide"
    assert [request.url.path for request in requests] == [
        "/v1/videos/generations",
        "/v1/videos/video_task_123",
        "/v1/videos/video_task_123/content",
    ]


async def test_provider_http_client_is_reused_and_recreated_after_close():
    first = get_provider_http_client("argolink")
    second = get_provider_http_client("argolink")

    assert first is second
    assert first.is_closed is False

    await close_provider_http_clients()
    assert first.is_closed is True

    third = get_provider_http_client("argolink")
    assert third is not first
    assert third.is_closed is False

    await close_provider_http_clients()


async def test_argolink_rate_limit_preserves_retry_after_hint():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "17"},
            json={"error": "rate limited"},
            request=request,
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(base_url="https://argolink.io", transport=transport) as client:
        adapter = ArgoLinkAdapter(base_url="https://argolink.io", api_key="test-key", client=client)
        with pytest.raises(ProviderAdapterError) as exc_info:
            await adapter.submit_generation(
                ProviderGenerationRequest(
                    generation_id="generation-rate-limit",
                    model_slug="seedance-2.5",
                    mode="text_to_video",
                    resolution="720p",
                    prompt="rate limit",
                    duration_seconds=5,
                )
            )

    error = exc_info.value
    assert error.public_code == "provider_temporarily_unavailable"
    assert error.raw_error == "argolink_rate_limited"
    assert error.retryable is True
    assert error.retry_after_seconds == 17.0


async def test_argolink_submit_read_timeout_is_not_replayed_automatically():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("submit response timed out", request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(base_url="https://argolink.io", transport=transport) as client:
        adapter = ArgoLinkAdapter(base_url="https://argolink.io", api_key="test-key", client=client)
        with pytest.raises(ProviderAdapterError) as exc_info:
            await adapter.submit_generation(
                ProviderGenerationRequest(
                    generation_id="generation-timeout",
                    model_slug="seedance-2.5",
                    mode="text_to_video",
                    resolution="720p",
                    prompt="ambiguous timeout",
                    duration_seconds=5,
                )
            )

    error = exc_info.value
    assert error.public_code == "provider_temporarily_unavailable"
    assert error.raw_error == "argolink_submit_outcome_unknown"
    assert error.retryable is False


async def test_argolink_missing_task_id_is_not_replayed_automatically():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"status": "accepted"}, request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(base_url="https://argolink.io", transport=transport) as client:
        adapter = ArgoLinkAdapter(base_url="https://argolink.io", api_key="test-key", client=client)
        with pytest.raises(ProviderAdapterError) as exc_info:
            await adapter.submit_generation(
                ProviderGenerationRequest(
                    generation_id="generation-missing-id",
                    model_slug="seedance-2.5",
                    mode="text_to_video",
                    resolution="720p",
                    prompt="missing task id",
                    duration_seconds=5,
                )
            )

    error = exc_info.value
    assert error.raw_error == "missing_provider_task_id"
    assert error.retryable is False
