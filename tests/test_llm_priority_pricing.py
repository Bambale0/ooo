import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from test_native_inference import setup

from app.generations.models import Generation
from app.inference.accounting import charges
from app.inference.service import quote

LUNA_COSTS = {
    "input_tokens": Decimal(".16"),
    "cached_input_tokens": Decimal(".016"),
    "cache_write_tokens": Decimal(".20"),
    "output_tokens": Decimal(".96"),
}


def token_prices(costs):
    return [(mode, "default", "million_tokens", cost * Decimal("110"), cost) for mode, cost in costs.items()]


@pytest.mark.parametrize("tier,multiplier", [(None, 1), ("default", 1), ("auto", 1), ("priority", 2), ("fast", 2)])
async def test_luna_service_tier_preserves_scaled_rates_and_actual_usage(
    client, db_session, monkeypatch, tier, multiplier
):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}],
                "usage": {
                    "input_tokens": 12000,
                    "input_tokens_details": {"cached_tokens": 4000},
                    "output_tokens": 1500,
                },
            },
        )

    partner, headers, upstream = await setup(
        db_session, monkeypatch, handler, model="gpt-5.6-luna", rates=token_prices(LUNA_COSTS)
    )
    body = {"model": "gpt-5.6-luna", "input": "Describe a winter morning.", "max_output_tokens": 2000}
    if tier is not None:
        body["service_tier"] = tier
    try:
        response = await client.post("/v1/responses", headers=headers, json=body)
        assert response.status_code == 200, response.text
        generation = (await db_session.execute(select(Generation))).scalar_one()
        assert generation.status == "completed"
        assert generation.usage_snapshot == {
            "input_tokens": 8000,
            "cached_input_tokens": 4000,
            "cache_write_tokens": 0,
            "output_tokens": 1500,
        }
        for mode, cost in LUNA_COSTS.items():
            rate = generation.request_payload["rates"][mode]
            assert Decimal(rate["cost"]) == cost * multiplier
            assert Decimal(rate["retail"]) == cost * Decimal("110") * multiplier
        assert generation.actual_provider_cost_usdt == Decimal(".002784") * multiplier
        assert generation.actual_charge_rub == (Decimal(".31") if multiplier == 1 else Decimal(".61"))
        reserved_charge, reserved_cost = charges(
            generation.request_payload["rates"],
            generation.request_payload["reserved_units"],
            divisor=Decimal("1000000"),
        )
        assert (generation.partner_price_rub, generation.provider_cost_usdt_snapshot) == (
            reserved_charge,
            reserved_cost,
        )
        await db_session.refresh(partner)
        assert partner.balance_rub == Decimal("1000000") - generation.actual_charge_rub
        assert calls == [body]
    finally:
        await upstream.aclose()


async def test_glm_documented_free_cache_creation_settles_without_a_synthetic_charge(client, db_session, monkeypatch):
    costs = {
        "input_tokens": Decimal(".12"),
        "cached_input_tokens": Decimal(".024"),
        "cache_write_tokens": Decimal("0"),
        "output_tokens": Decimal(".40"),
    }

    def handler(request):
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "OK"}],
                "usage": {
                    "input_tokens": 1000,
                    "cache_read_input_tokens": 500,
                    "cache_creation_input_tokens": 12000,
                    "output_tokens": 1000,
                },
            },
        )

    partner, headers, upstream = await setup(
        db_session, monkeypatch, handler, model="glm-5.3-flash", rates=token_prices(costs)
    )
    try:
        response = await client.post(
            "/v1/messages",
            headers=headers,
            json={"model": "glm-5.3-flash", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 2000},
        )
        assert response.status_code == 200, response.text
        generation = (await db_session.execute(select(Generation))).scalar_one()
        assert generation.status == "completed"
        assert generation.usage_snapshot["cache_write_tokens"] == 12000
        assert Decimal(generation.request_payload["rates"]["cache_write_tokens"]["retail"]) == 0
        assert Decimal(generation.request_payload["rates"]["cache_write_tokens"]["cost"]) == 0
        assert generation.actual_provider_cost_usdt == Decimal(".000532")
        assert generation.actual_charge_rub == Decimal(".06")
        await db_session.refresh(partner)
        assert partner.balance_rub == Decimal("999999.94")
    finally:
        await upstream.aclose()


@pytest.mark.parametrize("tier", ["unknown", ["priority"], {"name": "priority"}])
def test_unknown_service_tier_cannot_trigger_internal_error_or_priority_billing(tier):
    prices = [
        SimpleNamespace(mode=mode, resolution=res, billing_unit=unit, price_rub=retail, provider_cost_usdt=cost)
        for mode, res, unit, retail, cost in token_prices(LUNA_COSTS)
    ]
    try:
        rates, _, _ = quote(
            "responses", {"model": "gpt-5.6-luna", "input": "Hi", "service_tier": tier}, prices, fx=Decimal("100")
        )
    except HTTPException as exc:
        assert exc.status_code in {400, 422}
    else:
        assert Decimal(rates["output_tokens"]["cost"]) == LUNA_COSTS["output_tokens"]
