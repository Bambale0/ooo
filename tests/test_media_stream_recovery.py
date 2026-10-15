import asyncio

import httpx
import pytest

from app.providers.media_stream import resumable_media_body


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks, fail=False):
        self.chunks, self.fail, self.closed = chunks, fail, False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.fail:
            raise httpx.RemoteProtocolError("upstream disconnected")

    async def aclose(self):
        self.closed = True


async def run_case(*, first_headers=None, resumed_headers=None, resumed_status=206, fail_second=False):
    calls, streams = [], []
    headers = {"content-length": "6", "etag": '"version1"', "accept-ranges": "bytes"}
    headers.update(first_headers or {})

    def handler(request):
        calls.append(request)
        assert "authorization" not in request.headers and "cookie" not in request.headers
        if len(calls) == 1:
            stream = Body([b"abc"], True)
            streams.append(stream)
            return httpx.Response(200, headers=headers, stream=stream)
        assert request.headers["range"] == "bytes=3-5"
        assert request.headers["if-range"] == '"version1"'
        stream = Body([] if fail_second else [b"def"], fail_second)
        streams.append(stream)
        reply = {"content-length": "3", "content-range": "bytes 3-5/6", "etag": '"version1"'}
        reply.update(resumed_headers or {})
        return httpx.Response(resumed_status, headers=reply, stream=stream)

    chunks = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers={"Authorization": "private"}, cookies={"a": "private"}
    ) as client:
        first = await client.send(httpx.Request("GET", "https://videos.tpkcur.xyz/result.mp4"), stream=True)
        try:
            async for chunk in resumable_media_body(client, first):
                chunks.append(chunk)
        finally:
            assert all(stream.closed for stream in streams)
            assert len(calls) <= 3
    return b"".join(chunks), calls


async def test_recovers_exact_bytes_without_client_credentials():
    data, calls = await run_case()
    assert data == b"abcdef" and len(calls) == 2


@pytest.mark.parametrize("headers", [{"etag": ""}, {"etag": 'W/"version1"'}, {"accept-ranges": "none"}])
async def test_no_resume_without_strong_identity(headers):
    with pytest.raises(httpx.RemoteProtocolError):
        await run_case(first_headers=headers)


@pytest.mark.parametrize("status", [200, 302, 403, 416, 500])
async def test_never_splices_invalid_status(status):
    with pytest.raises(httpx.RemoteProtocolError, match="media_resume_mismatch"):
        await run_case(resumed_status=status)


@pytest.mark.parametrize(
    "headers",
    [
        {"etag": '"changed"'},
        {"content-range": "bytes 0-2/6"},
        {"content-length": "4"},
        {"content-encoding": "gzip"},
    ],
)
async def test_never_splices_changed_or_misaligned_object(headers):
    with pytest.raises(httpx.RemoteProtocolError, match="media_resume_mismatch"):
        await run_case(resumed_headers=headers)


async def test_repeated_failure_is_bounded():
    with pytest.raises(httpx.RemoteProtocolError):
        await run_case(fail_second=True)


async def test_caller_range_is_not_rewritten():
    stream = Body([b"xyz"])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                206,
                headers={"content-length": "3", "content-range": "bytes 3-5/6"},
                stream=stream,
            )
        )
    ) as client:
        response = await client.send(httpx.Request("GET", "https://videos.tpkcur.xyz/v"), stream=True)
        assert b"".join([chunk async for chunk in resumable_media_body(client, response)]) == b"xyz"
    assert stream.closed


async def test_deadline_closes_stalled_stream(monkeypatch):
    class Stalled(Body):
        async def __aiter__(self):
            await asyncio.sleep(10)
            yield b"unused"

    stream = Stalled([])
    original_timeout = asyncio.timeout
    monkeypatch.setattr("app.providers.media_stream.asyncio.timeout", lambda value: original_timeout(0.001))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=stream))
    ) as client:
        response = await client.send(httpx.Request("GET", "https://videos.tpkcur.xyz/v"), stream=True)
        with pytest.raises(TimeoutError):
            async for _ in resumable_media_body(client, response):
                pass
    assert stream.closed


async def test_consumer_cancellation_closes_response():
    stream = Body([b"first", b"second"])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=stream))
    ) as client:
        response = await client.send(httpx.Request("GET", "https://videos.tpkcur.xyz/v"), stream=True)
        iterator = resumable_media_body(client, response)
        assert await anext(iterator) == b"first"
        await iterator.aclose()
    assert stream.closed
