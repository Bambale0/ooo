"""Provider image safety: full decode and bounded failure finality."""

import asyncio
import base64
import io
import struct
import zlib
from decimal import Decimal

import httpx
import pytest
from PIL import Image
from sqlalchemy import select
from test_native_inference import setup

from app.billing.models import LedgerEntry
from app.generations.models import Generation
from app.infrastructure.config import get_settings

MODEL = "seedream-5.0-pro"
RATES = [("default", tier, "generation", Decimal("3.85"), Decimal(".03")) for tier in ("1K", "2K")]


def png_with_invalid_pixels(width=1024, height=1024):
    def chunk(kind, payload):
        checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", b"invalid zlib stream") + chunk(b"IEND", b"")


@pytest.mark.parametrize("kind", ["invalid_pixels", "decompression_limit"])
async def test_seedream_unsafe_image_closes_without_charge(client, db_session, monkeypatch, kind):
    if kind == "invalid_pixels":
        content = png_with_invalid_pixels()
        # Prove the fixture specifically passes container verification but not decode.
        with Image.open(io.BytesIO(content)) as image:
            image.verify()
        with Image.open(io.BytesIO(content)) as image, pytest.raises(OSError):
            image.load()
    else:
        content = png_with_invalid_pixels(100000, 100000)
        with pytest.raises(Image.DecompressionBombError):
            Image.open(io.BytesIO(content))
    data = {"data": [{"b64_json": base64.b64encode(content).decode()}]}
    calls = []

    def upstream_response(request):
        calls.append(request)
        return httpx.Response(200, json=data)

    partner, headers, upstream = await setup(
        db_session, monkeypatch, upstream_response, model=MODEL, category="image", rates=RATES
    )
    body = {"model": MODEL, "prompt": "Cup", "resolution": "1k"}
    try:
        response = await client.post("/v1/images/generations", headers=headers, json=body)
        assert response.status_code == 503, response.text
        repeated = await client.post("/v1/images/generations", headers=headers, json=body)
        assert repeated.status_code == 503 and repeated.content == response.content
        generation = await db_session.scalar(select(Generation))
        assert generation.status == "failed" and generation.billing_status == "not_chargeable"
        assert generation.actual_charge_rub is None
        assert partner.balance_rub == Decimal("1000000")
        assert len(calls) == 1
        assert not list(await db_session.scalars(select(LedgerEntry)))
    finally:
        await upstream.aclose()


class DelayedImageBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        await asyncio.sleep(0.1)
        yield b"never decoded after deadline"


async def test_seedream_result_url_overall_deadline_is_terminal(client, db_session, monkeypatch):
    from app.infrastructure import public_http

    assert issubclass(TimeoutError, OSError)
    monkeypatch.setattr(get_settings(), "native_request_timeout_seconds", 0.02)
    monkeypatch.setattr(
        public_http,
        "public_http_client",
        lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=DelayedImageBody()))
        ),
    )
    partner, headers, upstream = await setup(
        db_session,
        monkeypatch,
        lambda request: httpx.Response(200, json={"data": [{"url": "https://media.example.org/result.png"}]}),
        model=MODEL,
        category="image",
        rates=RATES,
    )
    try:
        response = await client.post("/v1/images/generations", headers=headers, json={"model": MODEL, "prompt": "Cup"})
        assert response.status_code == 503
        generation = await db_session.scalar(select(Generation))
        assert generation.status == "failed" and generation.billing_status == "not_chargeable"
        assert generation.actual_charge_rub is None and partner.balance_rub == Decimal("1000000")
    finally:
        await upstream.aclose()
