import json

import httpx
import pytest

from app.providers.asale import AsaleAdapter, asale_supports_request
from app.providers.base import ProviderAdapterError, ProviderGenerationRequest


def request(**changes):
    values = {
        "generation_id": "gen-1",
        "model_slug": "seedance-2.0",
        "mode": "text_to_video",
        "resolution": "720p",
        "prompt": "test",
        "duration_seconds": 5,
        "aspect_ratio": "16:9",
    }
    values.update(changes)
    return ProviderGenerationRequest(**values)


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({}, True),
        ({"model_slug": "seedance-2.0", "resolution": "4k"}, True),
        ({"model_slug": "seedance-2.5", "resolution": "720p"}, True),
        ({"model_slug": "seedance-2.5", "resolution": "1080p"}, False),
        ({"model_slug": "seedance-2.5", "aspect_ratio": "9:21"}, False),
        (
            {
                "mode": "videos/generations",
                "native_body": {
                    "model": "seedance-2.0",
                    "prompt": "native",
                    "duration": 5,
                    "resolution": "720p",
                    "aspect_ratio": "16:9",
                },
            },
            True,
        ),
        (
            {
                "mode": "videos/generations",
                "native_body": {
                    "model": "seedance-2.0",
                    "prompt": "native",
                    "duration": 5,
                    "resolution": "720p",
                    "aspect_ratio": "16:9",
                    "frame_images": [
                        {"frame_type": "first_frame", "url": "https://example.test/start.jpg"}
                    ],
                },
            },
            False,
        ),
        (
            {
                "mode": "videos/generations",
                "native_body": {
                    "model": "seedance-2.0",
                    "prompt": "native",
                    "duration": 5,
                    "resolution": "720p",
                    "aspect_ratio": "16:9",
                    "seed": 42,
                },
            },
            False,
        ),
        ({"mode": "reference", "reference_images": ("https://example.test/ref.jpg",)}, False),
        ({"mode": "first_frame", "start_image": "https://example.test/start.jpg"}, False),
        ({"model_slug": "wan-3"}, False),
    ],
)
def test_asale_fallback_only_accepts_reviewed_equivalent_subset(changes, expected):
    assert asale_supports_request(request(**changes)) is expected


async def test_asale_submit_and_poll_video_contract():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        assert req.headers["Authorization"] == "Bearer sk-asale-test"
        if req.method == "POST":
            assert req.url.path == "/v1/videos"
            assert json.loads(req.content) == {
                "model": "seedance-2.0",
                "prompt": "test",
                "duration": 5,
                "resolution": "720p",
                "aspect_ratio": "16:9",
            }
            return httpx.Response(200, json={"id": "asale-task", "status": "pending"})
        assert req.url.path == "/v1/videos/asale-task"
        return httpx.Response(
            200,
            json={"id": "asale-task", "status": "completed", "billedSeconds": 5},
        )

    async with httpx.AsyncClient(
        base_url="https://gw.asale.ai",
        transport=httpx.MockTransport(handler),
    ) as client:
        adapter = AsaleAdapter(api_key="sk-asale-test", client=client)
        submitted = await adapter.submit_generation(request())
        assert submitted.provider_task_id == "asale-task"
        polled = await adapter.poll_generation("asale-task")

    assert polled.status == "completed"
    assert polled.result_url == "https://gw.asale.ai/v1/videos/asale-task/content"
    assert polled.usage == {"billed_seconds": 5}
    assert len(seen) == 2


async def test_asale_no_supply_is_retryable():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"code": "no_supply", "message": "none"}})

    async with httpx.AsyncClient(
        base_url="https://gw.asale.ai",
        transport=httpx.MockTransport(handler),
    ) as client:
        adapter = AsaleAdapter(api_key="sk-asale-test", client=client)
        with pytest.raises(ProviderAdapterError) as caught:
            await adapter.submit_generation(request())

    assert caught.value.public_code == "provider_temporarily_unavailable"
    assert caught.value.raw_error == "asale_no_supply"
    assert caught.value.retryable is True


@pytest.mark.parametrize("status,code", [(401, "unauthorized"), (402, "payment_required"), (403, "forbidden")])
async def test_asale_definite_account_rejection_is_not_ambiguous_submission(status, code):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": code, "message": code}})

    async with httpx.AsyncClient(
        base_url="https://gw.asale.ai",
        transport=httpx.MockTransport(handler),
    ) as client:
        adapter = AsaleAdapter(api_key="sk-asale-test", client=client)
        with pytest.raises(ProviderAdapterError) as caught:
            await adapter.submit_generation(request())

    assert caught.value.public_code == "provider_rejected_request"
    assert caught.value.retryable is False
