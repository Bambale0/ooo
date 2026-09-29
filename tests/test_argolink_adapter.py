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

    assert seen_authorization == ["Bearer valid-key", "Bearer invalid-contract-probe", "Bearer bad-key"]


async def test_argolink_adapter_submits_polls_and_streams_with_configured_key():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers.get("Authorization") == "Bearer test-key"
        if request.method == "POST" and request.url.path == "/v1/videos/generations":
            body = request.read().decode("utf-8")
            assert "seedance-2.5" in body
            assert "metadata" not in body
            assert '"duration":5' in body
            assert '"aspect_ratio":"9:16"' in body
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
                mode="reference",
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


@pytest.mark.parametrize("status", [200, 302, 400, 401, 403, 404, 429, 500, 503])
async def test_key_probe_rejects_outage_or_identical_invalid_key_response(status):
    async with httpx.AsyncClient(
        base_url="https://argolink.io",
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json={})),
    ) as client:
        assert await ArgoLinkAdapter(client=client).validate_key("candidate") is False


@pytest.mark.parametrize("status", [408, 500, 502, 503, 504])
async def test_ambiguous_submit_server_error_is_not_replayed(status):
    async with httpx.AsyncClient(
        base_url="https://argolink.io",
        transport=httpx.MockTransport(lambda request: httpx.Response(status)),
    ) as client:
        with pytest.raises(ProviderAdapterError) as exc:
            await ArgoLinkAdapter(api_key="test-key", client=client).submit_generation(
                ProviderGenerationRequest(
                    generation_id="test",
                    model_slug="seedance-2.5",
                    mode="text_to_video",
                    resolution="720p",
                    prompt="test",
                    duration_seconds=5,
                )
            )
        assert exc.value.retryable is False


@pytest.mark.parametrize("data", [[], {}, {"status": "surprise"}])
async def test_poll_rejects_malformed_or_unknown_status(data):
    async with httpx.AsyncClient(
        base_url="https://argolink.io",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data)),
    ) as client:
        with pytest.raises(ProviderAdapterError):
            await ArgoLinkAdapter(api_key="test-key", client=client).poll_generation("task-1")


async def test_content_redirect_does_not_reach_private_network():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})

    async with httpx.AsyncClient(base_url="https://argolink.io", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderAdapterError):
            await ArgoLinkAdapter(api_key="test-key", client=client).open_result_stream(
                "https://argolink.io/v1/videos/task-1/content",
            )
    assert len(requests) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://argolink.io/v1/videos/../admin/content",
        "https://argolink.io/v1/videos/id/content?url=http://localhost",
        "https://argolink.io.evil.test/v1/videos/id/content",
    ],
)
async def test_content_url_must_match_exact_protected_path(url):
    with pytest.raises(ProviderAdapterError):
        await ArgoLinkAdapter(api_key="test-key").open_result_stream(url)


async def test_grok_video_uses_actual_video_duration_when_usage_has_only_ticks():
    import httpx

    from app.providers.argolink import ArgoLinkAdapter

    def handler(request):
        return httpx.Response(
            200,
            json={
                "request_id": "private-job",
                "model": "grok-imagine-video-1.5",
                "status": "done",
                "video": {"duration": 3},
                "usage": {"cost_in_usd_ticks": 900000000},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://argolink.io") as client:
        result = await ArgoLinkAdapter(api_key="test", client=client).poll_generation("private-job")
    assert result.usage == {"billed_seconds": 3, "output_seconds": 3, "reference_video_seconds": 0}
