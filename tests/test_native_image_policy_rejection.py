"""An explicit provider policy refusal is terminal even with HTTP 200."""
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from test_native_inference import setup
from test_seedream5_pro import rendered

from app.billing.models import LedgerEntry
from app.generations.models import Generation
from app.providers.models import ProviderAttempt

RATES = [("default", tier, "generation", Decimal("3.85"), Decimal(".03")) for tier in ("1K", "2K")]


@pytest.mark.parametrize("protocol", ["images/generations", "images/edits"])
@pytest.mark.parametrize("with_result", [False, True])
async def test_http_200_policy_refusal_is_terminal_unbilled_and_replayed(
    client, db_session, monkeypatch, protocol, with_result
):
    calls = []
    private = "private prompt https://private.example/photo?token=secret-value"

    def respond(request):
        calls.append(request)
        payload = {"error": {"code": "content_policy_violation", "message": private}}
        if with_result:
            payload["data"] = [rendered()]
        return httpx.Response(200, json=payload)

    partner, headers, upstream = await setup(
        db_session, monkeypatch, respond, model="seedream-5.0-pro", category="image", rates=RATES
    )
    body = {"model": "seedream-5.0-pro", "prompt": "Cup", "resolution": "1k"}
    if protocol.endswith("edits"):
        body["images"] = [{"image_url": "https://example.org/input.jpg"}]
    try:
        response = await client.post("/v1/" + protocol, headers=headers, json=body)
        assert response.status_code == 422, response.text
        assert response.json()["error"]["type"] == "content_policy_violation"
        assert private not in response.text and "secret-value" not in response.text
        replay = await client.post("/v1/" + protocol, headers=headers, json=body)
        assert replay.status_code == 422 and replay.content == response.content
        assert len(calls) == 1
        generation = await db_session.scalar(select(Generation))
        attempt = await db_session.scalar(select(ProviderAttempt))
        assert generation.status == "failed" and generation.billing_status == "not_chargeable"
        assert generation.public_error_code == "content_policy_violation"
        assert generation.actual_charge_rub is None
        assert partner.balance_rub == Decimal("1000000")
        assert not list(await db_session.scalars(select(LedgerEntry)))
        # A refusal may follow provider computation: do not invent a free upstream cost.
        assert attempt.cost_status == "unknown" and attempt.provider_cost_usdt is None
        assert generation.provider_cost_hold_usdt > 0
        assert "content_policy_violation" in attempt.raw_error
        assert "secret-value" not in attempt.raw_error
    finally:
        await upstream.aclose()


@pytest.mark.parametrize("error", [{"code": "unknown_provider_error"}, "content_policy_violation", None])
async def test_unknown_or_malformed_success_envelope_stays_ambiguous(
    client, db_session, monkeypatch, error
):
    partner, headers, upstream = await setup(
        db_session, monkeypatch, lambda request: httpx.Response(200, json={"error": error}),
        model="seedream-5.0-pro", category="image", rates=RATES
    )
    try:
        response = await client.post(
            "/v1/images/generations", headers=headers,
            json={"model": "seedream-5.0-pro", "prompt": "Cup", "resolution": "1k"}
        )
        assert response.status_code == 503
        assert response.json()["error"]["type"] == "provider_response_invalid"
        attempt = await db_session.scalar(select(ProviderAttempt))
        assert attempt.cost_status == "unknown"
        assert partner.balance_rub == Decimal("1000000")
    finally:
        await upstream.aclose()
