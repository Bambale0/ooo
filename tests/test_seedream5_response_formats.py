"""Requested result representation is part of the delivery contract."""

import base64
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from test_native_inference import setup
from test_seedream5_pro import rendered

from app.billing.models import LedgerEntry
from app.generations.models import Generation
from app.infrastructure import public_http


@pytest.mark.parametrize(
    "requested,representation",
    [(None, "url"), ("b64_json", "url"), ("url", "b64_json"), (None, "empty_b64")],
)
async def test_wrong_seedream_response_format_is_not_billable(
    client, db_session, monkeypatch, requested, representation
):
    raw = base64.b64decode(rendered()["b64_json"])
    reads = []

    def download(request):
        reads.append(request)
        return httpx.Response(200, content=raw)

    monkeypatch.setattr(
        public_http, "public_http_client", lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(download))
    )
    item = rendered() if representation == "b64_json" else {"url": "https://media.example.org/image.png"}
    if representation == "empty_b64":
        item["b64_json"] = ""
    rates = [("default", tier, "generation", Decimal("3.85"), Decimal(".03")) for tier in ("1K", "2K")]
    partner, headers, upstream = await setup(
        db_session,
        monkeypatch,
        lambda request: httpx.Response(200, json={"data": [item]}),
        model="seedream-5.0-pro",
        category="image",
        rates=rates,
    )
    body = {"model": "seedream-5.0-pro", "prompt": "Cup"}
    if requested is not None:
        body["response_format"] = requested
    try:
        response = await client.post("/v1/images/generations", headers=headers, json=body)
        assert response.status_code == 503, response.text
        generation = await db_session.scalar(select(Generation))
        assert generation.status == "failed" and generation.billing_status == "not_chargeable"
        assert generation.actual_charge_rub is None and partner.balance_rub == Decimal("1000000")
        assert not reads and not list(await db_session.scalars(select(LedgerEntry)))
    finally:
        await upstream.aclose()
