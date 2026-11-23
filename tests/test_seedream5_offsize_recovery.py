"""Prevent successful Seedream image-edit responses from being discarded on size drift."""

import base64
import io
from decimal import Decimal

import httpx
import pytest
from PIL import Image
from sqlalchemy import select
from test_native_inference import setup

from app.billing.models import LedgerEntry
from app.generations.models import Generation
from app.providers.models import ProviderAttempt

MODEL = "seedream-5.0-pro"
RATES = [
    ("default", tier, "generation", Decimal("3.85"), Decimal(".03"))
    for tier in ("1K", "2K")
]


def jpeg_payload(width: int, height: int) -> dict:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height)).save(buffer, format="JPEG")
    return {"data": [{"b64_json": base64.b64encode(buffer.getvalue()).decode("ascii")}]}


@pytest.mark.asyncio
async def test_completed_seedream_offsize_result_is_delivered_unbilled_with_reconciliation(
    client, db_session, monkeypatch
):
    """A provider-success JPEG with near-identical ratio must reach KSU, not 503."""
    expected = jpeg_payload(1024, 1365)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=expected)

    partner, headers, upstream = await setup(
        db_session, monkeypatch, handler, model=MODEL, category="image", rates=RATES
    )
    payload = {
        "model": MODEL,
        "prompt": "Keep the portrait, adjust the light",
        "images": [{"image_url": "https://reference.example/portrait.jpg"}],
        "aspect_ratio": "3:4",
        "resolution": "1k",
        "response_format": "b64_json",
        "n": 1,
    }
    try:
        a = await client.post("/v1/images/edits", headers=headers, json=payload)
        assert a.status_code == 200, a.text[:300]
        assert a.json()["data"] == expected["data"]
        replay = await client.post("/v1/images/edits", headers=headers, json=payload)
        assert replay.status_code == 200
        assert replay.content == a.content
        assert len(calls) == 1
        g = (await db_session.scalars(select(Generation))).one()
        attempt = (await db_session.scalars(select(ProviderAttempt))).one()
        assert g.status == attempt.status == "completed"
        assert g.request_payload["usage_reconciliation_pending"] is True
        assert g.actual_charge_rub is None
        assert attempt.cost_status == "unknown"
        assert not list(await db_session.scalars(select(LedgerEntry)))
        await db_session.refresh(partner)
        assert partner.balance_rub == Decimal("1000000")
    finally:
        await upstream.aclose()


@pytest.mark.asyncio
async def test_seedream_wrong_ratio_is_delivered_unbilled_without_changing_request(
    client, db_session, monkeypatch
):
    output = jpeg_payload(1280, 960)
    partner, headers, upstream = await setup(
        db_session,
        monkeypatch,
        lambda request: httpx.Response(200, json=output),
        model=MODEL,
        category="image",
        rates=RATES,
    )
    try:
        a = await client.post("/v1/images/generations", headers=headers, json={
            "model": MODEL, "prompt": "A scene", "resolution": "1k",
            "aspect_ratio": "3:4", "n": 1,
        })
        assert a.status_code == 200, a.text[:300]
        assert a.json()["data"] == output["data"]
        g = (await db_session.scalars(select(Generation))).one()
        attempt = (await db_session.scalars(select(ProviderAttempt))).one()
        assert g.status == attempt.status == "completed"
        assert g.request_payload["usage_reconciliation_pending"] is True
        assert g.actual_charge_rub is None
        assert attempt.cost_status == "unknown"
        assert not list(await db_session.scalars(select(LedgerEntry)))
    finally:
        await upstream.aclose()
