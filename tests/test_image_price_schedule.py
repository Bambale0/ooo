import base64
import io
import json
from decimal import Decimal

import httpx
import pytest
from PIL import Image
from sqlalchemy import select
from test_native_inference import setup

from app.billing.models import LedgerEntry
from app.generations.models import Generation
from app.inference.accounting import settle_actual


def rendered_image(edge):
    image = io.BytesIO()
    Image.new("RGB", (edge, 1)).save(image, format="PNG")
    return {"b64_json": base64.b64encode(image.getvalue()).decode()}


@pytest.mark.parametrize("model", ["nano-banana-pro", "gpt-image-2.5-sunburst"])
@pytest.mark.parametrize("tier,edge", [("1K", 1024), ("2K", 2048), ("4K", 4096)])
@pytest.mark.parametrize("requested,returned", [(1, 1), (2, 2), (3, 1)])
async def test_flat_image_price_reserves_then_charges_returned_count_once(
    client, db_session, monkeypatch, model, tier, edge, requested, returned
):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [rendered_image(edge) for _ in range(returned)]})

    costs = {"1K": ".03", "2K": ".03", "4K": ".03"}
    if model.startswith("gpt-image"):
        costs = {"1K": ".015", "2K": ".015", "4K": ".02"}
    prices = [
        ("default", resolution, "generation", Decimal("3.50"), Decimal(cost)) for resolution, cost in costs.items()
    ]
    partner, headers, upstream = await setup(
        db_session, monkeypatch, handler, model=model, category="image", rates=prices
    )
    body = {"model": model, "prompt": "One studio product photo", "resolution": tier, "n": requested}
    try:
        response = await client.post("/v1/images/generations", headers=headers, json=body)
        assert response.status_code == 200, response.text
        generation = (await db_session.execute(select(Generation))).scalar_one()
        assert generation.status == "completed"
        assert generation.partner_price_rub == Decimal("3.50") * requested
        assert generation.actual_charge_rub == Decimal("3.50") * returned
        assert generation.actual_provider_cost_usdt == Decimal(costs[tier]) * returned
        assert generation.usage_snapshot == {tier: returned}
        await db_session.refresh(partner)
        assert partner.balance_rub == Decimal("1000000") - Decimal("3.50") * returned
        ledger = list((await db_session.execute(select(LedgerEntry))).scalars())
        assert sum((item.amount_rub for item in ledger), Decimal(0)) == -Decimal("3.50") * returned

        duplicate = await client.post("/v1/images/generations", headers=headers, json=body)
        assert duplicate.status_code == 409
        await settle_actual(db_session, generation, {tier: returned})
        assert calls == [body]
        assert len(list((await db_session.execute(select(LedgerEntry))).scalars())) == len(ledger)
    finally:
        await upstream.aclose()


async def test_gpt_equal_retail_tiers_reserve_maximum_possible_procurement(client, db_session, monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"data": [rendered_image(4096)]})

    prices = [
        ("default", tier, "generation", Decimal("3.50"), Decimal(cost))
        for tier, cost in [("1K", ".015"), ("2K", ".015"), ("4K", ".02")]
    ]
    _, headers, upstream = await setup(
        db_session, monkeypatch, handler, model="gpt-image-2.5-sunburst", category="image", rates=prices
    )
    try:
        # A requested 1K is not a trustworthy bound on actual GPT image dimensions.
        response = await client.post(
            "/v1/images/generations",
            headers=headers,
            json={"model": "gpt-image-2.5-sunburst", "prompt": "A product photo", "resolution": "1K"},
        )
        assert response.status_code == 200, response.text
        generation = (await db_session.execute(select(Generation))).scalar_one()
        assert generation.provider_cost_usdt_snapshot == Decimal(".02")
        assert generation.provider_cost_reserve_rub == Decimal("2.00")
        assert generation.actual_provider_cost_usdt == Decimal(".02")
        assert generation.actual_charge_rub == Decimal("3.50")
        assert generation.usage_snapshot == {"4K": 1}
    finally:
        await upstream.aclose()
