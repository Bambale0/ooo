"""Bounded, byte-exact recovery of an interrupted immutable CDN response."""

import asyncio
import re

import httpx


async def resumable_media_body(client: httpx.AsyncClient, response: httpx.Response):
    """Resume full responses only when a strong validator proves object identity.

    Never replay a partial body as a new 200 response, follow redirects, or copy
    client credentials to the CDN. Range requests from callers pass through.
    """
    current = response
    length = response.headers.get("content-length", "")
    etag = response.headers.get("etag", "")
    total = int(length) if length.isascii() and length.isdigit() else None
    resumable = (
        response.status_code == 200
        and total is not None
        and re.fullmatch(r'"[\x21\x23-\x7e]+"', etag) is not None
        and response.headers.get("accept-ranges", "").lower() == "bytes"
        and response.headers.get("content-encoding", "identity").lower() == "identity"
    )
    sent = 0
    retries = 0
    try:
        async with asyncio.timeout(180):
            if not resumable:
                async for chunk in current.aiter_bytes():
                    yield chunk
                return
            while True:
                try:
                    async for chunk in current.aiter_raw():
                        if total is not None and sent + len(chunk) > total:
                            raise httpx.RemoteProtocolError("media_length_mismatch")
                        sent += len(chunk)
                        yield chunk
                    if total is not None and sent != total:
                        raise httpx.RemoteProtocolError("media_incomplete")
                    return
                except (httpx.TransportError, httpx.StreamError):
                    if not resumable or retries >= 2 or sent >= total:
                        raise
                    retries += 1
                    await current.aclose()
                    # Keep the same vetted URL. No task resubmit or URL rewrite.
                    current = await client.send(
                        httpx.Request(
                            "GET",
                            response.request.url,
                            headers={
                                "Accept-Encoding": "identity",
                                "Range": f"bytes={sent}-{total - 1}",
                                "If-Range": etag,
                            },
                        ),
                        stream=True,
                        follow_redirects=False,
                    )
                    expected_range = f"bytes {sent}-{total - 1}/{total}"
                    if (
                        current.status_code != 206
                        or current.headers.get("content-range") != expected_range
                        or current.headers.get("etag") != etag
                        or current.headers.get("content-length") != str(total - sent)
                        or current.headers.get("content-encoding", "identity").lower() != "identity"
                    ):
                        raise httpx.RemoteProtocolError("media_resume_mismatch") from None
    finally:
        await current.aclose()
