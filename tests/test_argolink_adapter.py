import httpx

from app.providers.argolink import ArgoLinkAdapter
from app.providers.base import ProviderGenerationRequest


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


async def test_argolink_adapter_submits_polls_and_fetches_with_configured_key():
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
            return httpx.Response(200, content=b"video-bytes", headers={"content-type": "video/mp4"})
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
        content, content_type = await adapter.fetch_result_content(poll_result.result_url or "")

    assert result.provider_task_id == "video_task_123"
    assert poll_result.status == "completed"
    assert poll_result.result_url == "https://argolink.io/v1/videos/video_task_123/content"
    assert content == b"video-bytes"
    assert content_type == "video/mp4"
    assert [request.url.path for request in requests] == [
        "/v1/videos/generations",
        "/v1/videos/video_task_123",
        "/v1/videos/video_task_123/content",
    ]
