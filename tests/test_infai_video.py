import json
from decimal import Decimal

import httpx
import pytest

from app.providers.base import ProviderAdapterError, ProviderGenerationRequest
from app.providers.registry import get_provider_adapter


def request(**changes):
    native = {
        "model": "seedance-2.5",
        "prompt": "Animate the six references in order",
        "duration": 15,
        "resolution": "720p",
        "aspect_ratio": "9:16",
        "generate_audio": True,
        "omni_reference_task_type": "reference",
        "reference_images": [{"url": f"https://example.com/{i}.png"} for i in range(6)],
    }
    native.update(changes)
    return ProviderGenerationRequest(
        generation_id="test",
        model_slug=native["model"],
        mode="videos/generations",
        prompt=native["prompt"],
        resolution=native["resolution"],
        duration_seconds=native["duration"],
        aspect_ratio=native["aspect_ratio"],
        reference_images=tuple(item["url"] for item in native["reference_images"]),
        native_body=native,
    )


def test_infai_inference_registered_separately_from_management():
    assert get_provider_adapter("infai", api_key="business-key").provider_name == "infai"


async def test_infai_preserves_audio_and_six_ordered_references():
    from app.providers.infai_video import InfaiVideoAdapter

    def handler(req):
        assert req.headers["Authorization"] == "Bearer business-key"
        assert "New-Api-User" not in req.headers
        body = json.loads(req.content)
        assert body == {
            "model": "doubao-seedance-2-5-260628",
            "content": [{"type": "text", "text": request().prompt}]
            + [
                {"type": "image_url", "image_url": item, "role": "reference_image"}
                for item in request().native_body["reference_images"]
            ],
            "duration": 15,
            "resolution": "720p",
            "ratio": "9:16",
            "generate_audio": True,
            "omni_reference_task_type": "reference",
        }
        return httpx.Response(200, json={"id": "cgt-example"})

    async with httpx.AsyncClient(base_url="https://infai.cc", transport=httpx.MockTransport(handler)) as client:
        result = await InfaiVideoAdapter(api_key="business-key", client=client).submit_generation(request())
    assert result.provider_task_id == "cgt-example"


@pytest.mark.parametrize(
    "changes",
    [
        {"model": "seedance-2.5-nsfw"},
        {"resolution": "1080p"},
        {"omni_reference_task_type": "edit"},
        {"reference_videos": [{"url": "https://example.com/v.mp4"}]},
        {"seed": 99},
        {"watermark": False},
        {"callback_url": "https://example.com/hook"},
        {"n": 2},
        {"reference_images": [{"url": "https://example.com/a.png", "character_id": "private"}]},
    ],
)
def test_infai_does_not_silently_drop_unsupported_controls(changes):
    from app.providers.infai_video import infai_supports_request

    assert not infai_supports_request(request(**changes))


@pytest.mark.parametrize(
    "response",
    [
        {"id": "cgt-example"},
        {"code": 0, "data": {"task_id": "cgt-example"}},
    ],
)
async def test_infai_documented_submit_envelopes(response):
    from app.providers.infai_video import InfaiVideoAdapter

    async with httpx.AsyncClient(
        base_url="https://infai.cc", transport=httpx.MockTransport(lambda req: httpx.Response(200, json=response))
    ) as client:
        assert (
            await InfaiVideoAdapter(api_key="key", client=client).submit_generation(request())
        ).provider_task_id == "cgt-example"


@pytest.mark.parametrize("failure", ["timeout", "bad_json", "missing_id", "http_500", "redirect"])
async def test_infai_ambiguous_paid_submit_is_never_retryable(failure):
    from app.providers.infai_video import InfaiVideoAdapter

    seen = []

    def handler(req):
        seen.append(req)
        if failure == "timeout":
            raise httpx.ReadTimeout("secret-body", request=req)
        if failure == "bad_json":
            return httpx.Response(200, text="secret-body")
        if failure == "http_500":
            return httpx.Response(500, text="secret-body")
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://example.com"})
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(base_url="https://infai.cc", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderAdapterError) as error:
            await InfaiVideoAdapter(api_key="key", client=client).submit_generation(request())
    assert len(seen) == 1
    assert not error.value.retryable
    assert error.value.public_code == "provider_temporarily_unavailable"
    assert "secret-body" not in error.value.raw_error


async def test_infai_poll_reports_actual_tokens_and_duration():
    from app.providers.infai_video import InfaiVideoAdapter

    async with httpx.AsyncClient(
        base_url="https://infai.cc",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={
                    "id": "cgt-example",
                    "status": "succeeded",
                    "duration": 15,
                    "content": {"video_url": "https://ark-acg-cn-beijing.tos-cn-beijing.volces.com/result.mp4"},
                    "usage": {"completion_tokens": 324000, "total_tokens": 324000},
                },
            )
        ),
    ) as client:
        result = await InfaiVideoAdapter(api_key="key", client=client).poll_generation("cgt-example")
    assert result.status == "completed"
    assert result.usage == {"billed_seconds": 15, "completion_tokens": 324000}
    assert result.result_url == "https://infai.cc/api/v3/contents/generations/tasks/cgt-example/content"


def test_infai_token_ceiling_is_a_procurement_bound_not_retail():
    from app.providers.infai_video import infai_cost_ceiling

    assert infai_cost_ceiling(request(), Decimal("9.0415")) == Decimal("3.254940")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/result.mp4",
        "https://169.254.169.254/result.mp4",
        "https://ark-acg-cn-beijing.tos-cn-beijing.volces.com.evil.example/result.mp4",
        "https://user@ark-acg-cn-beijing.tos-cn-beijing.volces.com/result.mp4",
    ],
)
async def test_infai_result_rejects_untrusted_storage_without_fetch(url):
    from app.providers.infai_video import InfaiVideoAdapter

    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"status": "succeeded", "content": {"video_url": url}})

    async with httpx.AsyncClient(base_url="https://infai.cc", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderAdapterError):
            await InfaiVideoAdapter(api_key="key", client=client).open_result_stream(
                "https://infai.cc/api/v3/contents/generations/tasks/job/content"
            )
    assert len(calls) == 1


async def test_infai_storage_receives_no_credentials_or_cookies_and_no_redirect():
    from app.providers.infai_video import InfaiVideoAdapter

    calls = []

    def handler(req):
        calls.append(req)
        if req.url.host == "infai.cc":
            return httpx.Response(
                200,
                json={
                    "status": "succeeded",
                    "content": {"video_url": "https://ark-acg-cn-beijing.tos-cn-beijing.volces.com/result.mp4"},
                },
            )
        assert "Authorization" not in req.headers and "Cookie" not in req.headers
        assert req.headers["Range"] == "bytes=0-4"
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/"})

    async with httpx.AsyncClient(
        base_url="https://infai.cc",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer must-not-leak"},
        cookies={"session": "secret"},
    ) as client:
        with pytest.raises(ProviderAdapterError):
            await InfaiVideoAdapter(api_key="key", client=client).open_result_stream(
                "https://infai.cc/api/v3/contents/generations/tasks/job/content", range_header="bytes=0-4"
            )
    assert len(calls) == 2
