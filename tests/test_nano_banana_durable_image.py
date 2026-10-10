"""Regression for synchronous image replies lost after upstream completion."""
import base64
import io
from decimal import Decimal
from urllib.parse import urlsplit

import httpx
from PIL import Image
from sqlalchemy import select

from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.media.models import MediaAsset
from tests.test_native_inference import setup


async def test_completed_nano_banana_image_can_be_recovered_after_lost_sync_reply(
    client, db_session, monkeypatch, tmp_path,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "media_storage_backend", "local")
    monkeypatch.setattr(settings, "media_local_storage_dir", str(tmp_path))
    image = io.BytesIO()
    Image.new("RGB", (3, 2), (32, 64, 128)).save(image, format="PNG")
    content = image.getvalue()
    encoded = base64.b64encode(content).decode()

    def handler(request):
        return httpx.Response(200, json={"data": [{"b64_json": encoded}]})

    rates = [
        ("default", tier, "generation", Decimal("2"), Decimal(".02"))
        for tier in ("1K", "2K", "4K")
    ]
    _partner, headers, upstream = await setup(
        db_session, monkeypatch, handler, model="nano-banana-2.1", category="image", rates=rates
    )
    headers = {**headers, "X-Client-Request-Id": "image-regression-1234"}
    response = await client.post(
        "/v1/images/generations", headers=headers,
        json={"model": "nano-banana-2.1", "prompt": "test", "n": 1, "resolution": "1k", "response_format": "b64_json"},
    )
    assert response.status_code == 200
    assert response.json()["data"][0]["b64_json"] == encoded
    generation = (await db_session.execute(select(Generation))).scalar_one()
    asset = (await db_session.execute(select(MediaAsset))).scalar_one()
    assert generation.status == "completed"
    assert asset.kind == "image" and asset.status == "stored"
    assert generation.result_url.endswith(f"/media/{asset.id}/content")
    assert asset.storage_key is not None
    assert (tmp_path / asset.storage_key).read_bytes() == content
    assert "b64_json" not in str(generation.request_payload)

    status = await client.get("/api/v1/generations/by-client-request-id/image-regression-1234", headers=headers)
    assert status.status_code == 200
    result = status.json()
    assert result["status"] == "completed" and result["result_url"].startswith("http")
    signed = await client.get(urlsplit(result["result_url"]).path)
    assert signed.status_code == 200
    assert signed.content == content
    assert signed.headers["content-type"].startswith("image/png")
    await upstream.aclose()


async def test_image_storage_failure_must_not_mark_paid_generation_completed(
    client, db_session, monkeypatch, tmp_path,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "media_storage_backend", "local")
    monkeypatch.setattr(settings, "media_local_storage_dir", str(tmp_path))
    image = io.BytesIO()
    Image.new("RGB", (2, 2)).save(image, format="PNG")
    encoded = base64.b64encode(image.getvalue()).decode()

    def handler(request):
        return httpx.Response(200, json={"data": [{"b64_json": encoded}]})

    from app.media.storage import LocalMediaStorage

    async def unavailable(self, **kwargs):
        raise OSError("test: disk full")

    monkeypatch.setattr(LocalMediaStorage, "put", unavailable)
    rates = [("default", tier, "generation", Decimal("2"), Decimal(".02")) for tier in ("1K", "2K", "4K")]
    partner, headers, upstream = await setup(
        db_session, monkeypatch, handler, model="nano-banana-2.1", category="image", rates=rates
    )
    response = await client.post(
        "/v1/images/generations", headers=headers,
        json={"model": "nano-banana-2.1", "prompt": "test", "n": 1, "resolution": "1k", "response_format": "b64_json"},
    )
    assert response.status_code == 503
    assert "b64_json" not in response.text
    generation = (await db_session.execute(select(Generation))).scalar_one()
    assert generation.status == "reconciliation_required"
    assert generation.actual_charge_rub is None
    assert generation.result_url is None
    assert not (await db_session.execute(select(MediaAsset))).scalars().all()
    await db_session.refresh(partner)
    assert partner.balance_rub < Decimal("1000000")  # held, not double-refunded or charged
    await upstream.aclose()


async def test_local_storage_rejects_path_traversal_and_writes_atomically(tmp_path):
    import pytest

    from app.media.storage import LocalMediaStorage, MediaStorageError

    storage = LocalMediaStorage(str(tmp_path))
    with pytest.raises(MediaStorageError):
        await storage.put(key="../outside.png", content=b"x", content_type="image/png")
    assert await storage.put(key="images/a.png", content=b"image", content_type="image/png") == "images/a.png"
    path = await storage.local_path("images/a.png")
    assert path and path.read_bytes() == b"image"
    assert list((tmp_path / "images").glob(".pending-*")) == []