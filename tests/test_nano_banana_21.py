from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.contracts.registry import MODELS, image_reference_limit, validate_request
from app.generations.models import Generation
from test_image_price_schedule import rendered_image
from test_native_inference import setup


MODEL = "nano-banana-2.1"
RATIOS = ("1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "5:4", "4:5", "21:9", "1:4", "4:1", "1:8", "8:1")


def test_new_nano_21_contract_prices_only_viable_two_ruble_tiers():
    entry = MODELS[MODEL]
    assert entry["category"] == "image"
    assert entry["endpoint"] == "/v1/images/generations"
    assert {t["label"]: Decimal(str(t["price"])) for t in entry["procurement"]["tiers"]} == {
        "1K": Decimal(".02"),
        "2K": Decimal(".02"),
    }
    # 4K procurement is $0.025 per image, exceeding 2 RUB at the reviewed FX.
    assert image_reference_limit(MODEL) == 14
    assert image_reference_limit(MODEL, multipart=True) == 14


@pytest.mark.parametrize("protocol", ["images/generations", "images/edits"])
@pytest.mark.parametrize("resolution", ["1k", "2k"])
@pytest.mark.parametrize("aspect_ratio", RATIOS)
def test_new_nano_21_keeps_documented_image_options(protocol, resolution, aspect_ratio):
    body = {
        "model": MODEL,
        "prompt": "A green cup on a table",
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "n": 4,
        "response_format": "b64_json",
    }
    if protocol == "images/edits":
        body["images"] = [{"image_url": f"https://example.org/{i}.jpg"} for i in range(14)]
    assert validate_request(protocol, body) == body


@pytest.mark.parametrize(
    "controls",
    [
        {"resolution": "4k"},
        {"resolution": "8k"},
        {"aspect_ratio": "2:1"},
        {"response_format": "url"},
        {"n": 0},
        {"n": 5},
        {"images": [{"image_url": "https://example.org/ref.jpg"}] * 15},
        {"size": "4096x4096"},
        {"size": "1024x1024"},
    ],
)
def test_new_nano_21_rejects_unpriced_or_provider_unsupported_controls(controls):
    with pytest.raises(ValueError):
        validate_request("images/edits", {"model": MODEL, "prompt": "Edit the cup", **controls})


@pytest.mark.parametrize("resolution", ["1k", "2k"])
@pytest.mark.parametrize("requested,returned", [(1, 1), (4, 2)])
async def test_new_nano_21_two_rubles_per_returned_image(
    client, db_session, monkeypatch, resolution, requested, returned
):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"data": [rendered_image(1024) for _ in range(returned)]})

    rates = [
        ("default", tier, "generation", Decimal("2.00"), Decimal(".02"))
        for tier in ("1K", "2K")
    ]
    partner, headers, upstream = await setup(
        db_session, monkeypatch, handler, model=MODEL, category="image", rates=rates
    )
    body = {"model": MODEL, "prompt": "A green cup on a table", "resolution": resolution, "n": requested}
    try:
        response = await client.post("/v1/images/generations", headers=headers, json=body)
        assert response.status_code == 200, response.text
        generation = (await db_session.execute(select(Generation))).scalar_one()
        assert generation.status == "completed"
        assert generation.partner_price_rub == Decimal("2.00") * requested
        assert generation.actual_charge_rub == Decimal("2.00") * returned
        assert generation.actual_provider_cost_usdt == Decimal(".02") * returned
        assert generation.usage_snapshot == {resolution.upper(): returned}
        await db_session.refresh(partner)
        assert partner.balance_rub == Decimal("1000000") - Decimal("2.00") * returned
        assert len(calls) == 1
    finally:
        await upstream.aclose()
