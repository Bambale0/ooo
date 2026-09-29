from collections.abc import AsyncIterator

import httpx
import pytest

from app.providers.argolink import ArgoLinkAdapter
from app.providers.base import ProviderAdapterError

PROVIDER_URL = "https://argolink.io/v1/videos/task-123"
CONTENT_URL = f"{PROVIDER_URL}/content"
CDN_HOST = "ark-acg-ap-southeast-1.tos-ap-southeast-1.volces.com"
CDN_URL = f"https://{CDN_HOST}/result/video.mp4?X-Tos-Signature=test-signature&X-Tos-Expires=3600"


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes, fail: bool = False) -> None:
        self.chunks = chunks
        self.fail = fail
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        if self.fail:
            raise httpx.ReadTimeout("upstream content stopped sending bytes")

    async def aclose(self) -> None:
        self.closed = True


class DefaultAuth(httpx.Auth):
    def auth_flow(self, request: httpx.Request):
        request.headers["X-Default-Auth"] = "test-shared-auth-secret"
        yield request


@pytest.mark.parametrize("client_auth", [None, httpx.BasicAuth("test-user", "test-password"), DefaultAuth()])
async def test_cdn_request_does_not_inherit_shared_client_secrets(client_auth):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "argolink.io":
            assert request.url.path == "/v1/videos/task-123"
            assert request.headers["Authorization"] == "Bearer test-partner-provider-key"
            return httpx.Response(200, json={"status": "done", "video": {"url": CDN_URL}})
        assert str(request.url) == CDN_URL
        assert not {
            "authorization",
            "proxy-authorization",
            "cookie",
            "x-api-key",
            "x-default-auth",
            "referer",
        }.intersection(request.headers)
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(200, content=b"video", headers={"Content-Type": "video/mp4"})

    async with httpx.AsyncClient(
        base_url="https://argolink.io",
        transport=httpx.MockTransport(handler),
        auth=client_auth,
        headers={
            "Authorization": "Bearer test-shared-key",
            "Cookie": "test_shared_cookie=value",
            "X-Api-Key": "test-shared-api-key",
            "Referer": "https://argolink.io/private/test",
        },
        cookies={"test_cookie_jar": "value"},
        params={"test_shared_query_secret": "value"},
    ) as client:
        stream = await ArgoLinkAdapter(api_key="test-partner-provider-key", client=client).open_result_stream(
            CONTENT_URL
        )
        assert b"".join([chunk async for chunk in stream.body]) == b"video"
    assert [request.url.host for request in requests] == ["argolink.io", CDN_HOST]
    assert all(request.method == "GET" for request in requests)


async def test_complete_cdn_object_bypasses_the_stalling_protected_content_proxy():
    requests = []
    video = bytes(range(256)) * 1024
    cdn_body = TrackedStream(video[:32768], video[32768:])

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if str(request.url) == PROVIDER_URL:
            return httpx.Response(200, json={"status": "done", "video": {"url": CDN_URL}})
        if str(request.url) == CONTENT_URL:
            return httpx.Response(200, stream=TrackedStream(video[:24576], fail=True))
        assert str(request.url) == CDN_URL
        return httpx.Response(200, stream=cdn_body, headers={"Content-Length": str(len(video))})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        stream = await ArgoLinkAdapter(api_key="test-key", client=client).open_result_stream(CONTENT_URL)
        assert b"".join([chunk async for chunk in stream.body]) == video
        assert stream.status_code == 200
        assert stream.content_length == len(video)
    assert [str(request.url) for request in requests] == [PROVIDER_URL, CDN_URL]
    assert cdn_body.closed


async def test_cdn_stream_preserves_partial_response_headers_and_closes_responses():
    responses = []
    content_body = TrackedStream(b"e", b"o12")

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == PROVIDER_URL:
            assert "Range" not in request.headers
            response = httpx.Response(200, json={"status": "done", "video": {"url": CDN_URL}})
        else:
            assert str(request.url) == CDN_URL
            assert request.headers["Range"] == "bytes=3-6"
            response = httpx.Response(
                206,
                stream=content_body,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": "4",
                    "Content-Range": "bytes 3-6/12",
                    "Accept-Ranges": "bytes",
                },
            )
        responses.append(response)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        stream = await ArgoLinkAdapter(api_key="test-key", client=client).open_result_stream(
            CONTENT_URL, range_header="bytes=3-6"
        )
        assert responses[0].is_closed
        assert not content_body.closed
        assert b"".join([chunk async for chunk in stream.body]) == b"eo12"
        assert (stream.status_code, stream.content_type, stream.content_length) == (206, "video/mp4", 4)
        assert (stream.content_range, stream.accept_ranges) == ("bytes 3-6/12", "bytes")
    assert all(response.is_closed for response in responses)
    assert content_body.closed


@pytest.mark.parametrize(
    "cdn_url",
    [
        f"http://{CDN_HOST}/video.mp4",
        f"https://{CDN_HOST}:444/video.mp4",
        f"https://{CDN_HOST}:0/video.mp4",
        f"https://{CDN_HOST}:invalid/video.mp4",
        f"https://{CDN_HOST}.evil.test/video.mp4",
        f"https://child.{CDN_HOST}/video.mp4",
        f"https://{CDN_HOST}./video.mp4",
        f"https://{CDN_HOST}@evil.test/video.mp4",
        f"https://test-user:test-password@{CDN_HOST}/video.mp4",
        f"https://{CDN_HOST}/video.mp4#fragment",
        f"//{CDN_HOST}/video.mp4",
        "https://127.0.0.1/video.mp4",
        "http://169.254.169.254/latest/meta-data/",
        "https://[::1]/video.mp4",
        "https://[malformed/video.mp4",
        None,
        {"url": CDN_URL},
    ],
)
async def test_untrusted_cdn_url_uses_only_the_authenticated_protected_endpoint(cdn_url):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "argolink.io"
        assert request.headers["Authorization"] == "Bearer test-key"
        if str(request.url) == PROVIDER_URL:
            return httpx.Response(200, json={"status": "done", "video": {"url": cdn_url}})
        assert str(request.url) == CONTENT_URL
        assert request.headers["Range"] == "bytes=0-3"
        return httpx.Response(206, content=b"vide", headers={"Content-Range": "bytes 0-3/5"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        stream = await ArgoLinkAdapter(api_key="test-key", client=client).open_result_stream(
            CONTENT_URL, range_header="bytes=0-3"
        )
        assert b"".join([chunk async for chunk in stream.body]) == b"vide"
    assert [str(request.url) for request in requests] == [PROVIDER_URL, CONTENT_URL]


@pytest.mark.parametrize("payload", [{"status": "processing", "video": {"url": CDN_URL}}, [], {"status": "done"}])
async def test_cdn_is_used_only_for_a_completed_result_with_a_video_url(payload):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if str(request.url) == PROVIDER_URL:
            return httpx.Response(200, json=payload)
        assert str(request.url) == CONTENT_URL
        return httpx.Response(200, content=b"video")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        stream = await ArgoLinkAdapter(api_key="test-key", client=client).open_result_stream(CONTENT_URL)
        assert b"".join([chunk async for chunk in stream.body]) == b"video"
    assert requests == [PROVIDER_URL, CONTENT_URL]


@pytest.mark.parametrize("status_code", [302, 403, 503])
async def test_cdn_error_or_redirect_closes_response_without_following_redirects(status_code):
    requests = []
    content_body = TrackedStream(b"error")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if str(request.url) == PROVIDER_URL:
            return httpx.Response(200, json={"status": "done", "video": {"url": CDN_URL}})
        assert str(request.url) == CDN_URL
        return httpx.Response(
            status_code, stream=content_body, headers={"Location": "http://169.254.169.254/latest/meta-data/"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(ProviderAdapterError):
            await ArgoLinkAdapter(api_key="test-key", client=client).open_result_stream(CONTENT_URL)
    assert requests == [PROVIDER_URL, CDN_URL]
    assert content_body.closed


@pytest.mark.parametrize("stop_early", [False, True])
async def test_cdn_stream_closes_on_read_failure_or_consumer_close(stop_early):
    content_body = TrackedStream(b"partial", fail=True)

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == PROVIDER_URL:
            return httpx.Response(200, json={"status": "done", "video": {"url": CDN_URL}})
        assert str(request.url) == CDN_URL
        return httpx.Response(200, stream=content_body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        stream = await ArgoLinkAdapter(api_key="test-key", client=client).open_result_stream(CONTENT_URL)
        assert await anext(stream.body) == b"partial"
        if stop_early:
            await stream.body.aclose()
        else:
            with pytest.raises(httpx.ReadTimeout):
                await anext(stream.body)
        assert content_body.closed
