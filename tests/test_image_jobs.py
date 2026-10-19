import base64
import hashlib
import io
from decimal import Decimal

import httpx
import pytest
from PIL import Image
from test_native_inference import setup

from app.infrastructure.config import get_settings


@pytest.fixture
def image_spool(monkeypatch, tmp_path):
    monkeypatch.setenv("IMAGE_JOBS_ENABLED", "true")
    monkeypatch.setenv("IMAGE_RESULT_STORAGE_DIR", str(tmp_path / "spool"))
    monkeypatch.setenv("IMAGE_RESULT_MIN_FREE_BYTES", "0")
    get_settings.cache_clear()
    yield tmp_path / "spool"
    get_settings.cache_clear()


async def image_setup(db, monkeypatch, handler):
    return await setup(
        db,
        monkeypatch,
        handler,
        model="nano-banana-pro",
        category="image",
        rates=[("default", tier, "generation", Decimal("10"), Decimal(".03")) for tier in ("1K", "2K", "4K")],
    )


def image_payload():
    buffer = io.BytesIO()
    Image.new("RGB", (1024, 1024)).save(buffer, format="JPEG")
    return {"data": [{"b64_json": base64.b64encode(buffer.getvalue()).decode()}]}


async def test_image_job_is_submitted_once_recovered_and_deleted_only_after_ack(
    client, db_session, monkeypatch, image_spool
):
    calls = []
    payload = image_payload()

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=payload)

    partner, headers, upstream = await image_setup(db_session, monkeypatch, handler)
    body = {"model": "nano-banana-pro", "prompt": "A blue ceramic cup", "resolution": "1k", "n": 1}
    accepted = await client.post("/v1/images/generations", json=body, headers={**headers, "Prefer": "respond-async"})
    assert accepted.status_code == 202
    assert calls == []
    request_id = accepted.json()["request_id"]
    duplicate = await client.post("/v1/images/generations", json=body, headers={**headers, "Prefer": "respond-async"})
    assert duplicate.json()["request_id"] == request_id
    from app.inference.image_jobs import process_image_job

    assert await process_image_job(db_session, request_id)
    assert len(calls) == 1
    assert not await process_image_job(db_session, request_id)
    result = await client.get(f"/v1/images/jobs/{request_id}/result", headers=headers)
    assert result.status_code == 200 and result.json() == payload
    assert hashlib.sha256(result.content).hexdigest() == result.headers["X-Result-SHA256"]
    again = await client.get(f"/v1/images/jobs/{request_id}/result", headers=headers)
    assert again.content == result.content and len(calls) == 1
    ack = await client.post(
        f"/v1/images/jobs/{request_id}/ack", headers=headers, json={"sha256": result.headers["X-Result-SHA256"]}
    )
    assert ack.status_code == 200 and ack.json()["status"] == "acknowledged"
    assert not list(image_spool.glob("*.result"))
    assert (await client.get(f"/v1/images/jobs/{request_id}/result", headers=headers)).status_code == 410
    duplicate = await client.post("/v1/images/generations", json=body, headers={**headers, "Prefer": "respond-async"})
    assert duplicate.json()["request_id"] == request_id
    assert len(calls) == 1
    await upstream.aclose()
