import httpx

from app.providers.argolink import ArgoLinkAdapter
from app.providers.base import ProviderGenerationRequest


async def test_argolink_adapter_uses_public_models_for_key_probe_and_video_generation_endpoint():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": []})
        if request.method == "POST" and request.url.path == "/v1/videos/generations":
            body = request.read().decode("utf-8")
            assert "seedance-2.5" in body
            assert "generation-1" in body
            assert "\"duration\":5" in body
            assert "\"aspect_ratio\":\"9:16\"" in body
            assert "https://cdn.example.test/reference.jpg" in body
            return httpx.Response(200, json={"request_id": "video_task_123"})
        if request.method == "GET" and request.url.path == "/v1/videos/video_task_123":
            return httpx.Response(200, json={"status": "done"})
        if request.method == "GET" and request.url.path == "/v1/videos/video_task_123/content":
            return httpx.Response(200, content=b"video-bytes", headers={"content-type": "video/mp4"})
        return httpx.Response(404, json={"error": "not found"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        base_url="https://argolink.io",
        transport=transport,
        headers={"Authorization": "Bearer test-key", "Content-Type": "application/json"},
    ) as client:
        adapter = ArgoLinkAdapter(
            base_url="https://argolink.io",
            api_key="test-key",
            client=client,
        )

        assert await adapter.validate_key("test-key") is True
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
        "/v1/models",
        "/v1/videos/generations",
        "/v1/videos/video_task_123",
        "/v1/videos/video_task_123/content",
    ]
