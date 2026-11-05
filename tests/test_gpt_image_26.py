"""GPT Image 2.6 request, billing and scoped registration regressions."""

import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from test_image_price_schedule import rendered_image
from test_native_inference import setup

from app.accounts.models import Partner
from app.billing.models import LedgerEntry
from app.catalog.models import Model, PartnerPrice, PartnerPriceHistory, PartnerPriceSnapshot
from app.contracts.registry import validate_request
from app.generations.models import Generation
from app.providers.models import ProviderModelCapability

MODELS = ("gpt-image-2.6-flare", "gpt-image-2.6-sunburst")


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("quality", ["auto", "low", "medium", "high", "xhigh", "max"])
@pytest.mark.parametrize("size", ["auto", "1024x1024", "1440x1440", "2048x1152"])
def test_documented_controls_are_forwarded_unchanged(model, quality, size):
    body = {"model": model, "prompt": "Cup", "quality": quality, "size": size, "n": 7}
    assert validate_request("images/generations", body) == body
    body["images"] = [{"image_url": "https://example.org/cup.png"}] * 16
    assert validate_request("images/edits", body) == body


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize(
    "controls",
    [
        {"size": "3840x2160"}, {"size": "2049x1024"}, {"size": "0x1024"},
        {"size": "1024x1024x1024"}, {"size": None}, {"quality": "ultra"},
        {"resolution": "4k"}, {"aspect_ratio": "16:9"}, {"n": 8}, {"n": True},
        {"images": [{"image_url": "https://example.org/cup.png"}] * 17},
    ],
)
def test_unsupported_controls_fail_before_admission(model, controls):
    with pytest.raises(ValueError):
        validate_request("images/generations", {"model": model, "prompt": "Cup", **controls})


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("protocol", ["images/generations", "images/edits"])
@pytest.mark.parametrize("edge,tier", [(1024, "1K"), (1440, "2K")])
async def test_actual_images_are_charged_once_with_35_percent_price(
    client, db_session, monkeypatch, model, protocol, edge, tier
):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [rendered_image(edge)]})

    partner, headers, upstream = await setup(
        db_session, monkeypatch, handler, model=model, category="image",
        rates=[("default", t, "generation", Decimal("2.03"), Decimal(".015")) for t in ("1K", "2K")],
    )
    body = {"model": model, "prompt": "Cup", "size": "auto", "quality": "max", "n": 3}
    if protocol.endswith("edits"):
        body["images"] = [{"image_url": "https://example.org/cup.png"}]
    try:
        response = await client.post(f"/v1/{protocol}", headers=headers, json=body)
        assert response.status_code == 200, response.text
        replay = await client.post(f"/v1/{protocol}", headers=headers, json=body)
        assert replay.status_code == 200 and replay.content == response.content
        generation = await db_session.scalar(select(Generation))
        assert generation.partner_price_rub == Decimal("6.09")
        assert generation.actual_charge_rub == Decimal("2.03")
        assert generation.actual_provider_cost_usdt == Decimal(".015")
        assert generation.usage_snapshot == {tier: 1}
        assert partner.balance_rub == Decimal("999997.97")
        assert await db_session.scalar(select(func.count()).select_from(LedgerEntry)) == 1
        assert calls == [body]
    finally:
        await upstream.aclose()


@pytest.mark.parametrize("model", MODELS)
async def test_multipart_edit_preserves_quality_and_files(client, db_session, monkeypatch, model):
    calls = []

    def handler(request):
        calls.append(request.content)
        return httpx.Response(200, json={"data": [rendered_image(1024)]})

    _, headers, upstream = await setup(
        db_session, monkeypatch, handler, model=model, category="image",
        rates=[("default", t, "generation", Decimal("2.03"), Decimal(".015")) for t in ("1K", "2K")],
    )
    try:
        response = await client.post(
            "/v1/images/edits", headers=headers,
            data={"model": model, "prompt": "Recolor", "quality": "xhigh", "size": "1024x1024"},
            files=[("image[]", ("cup.png", b"reference-bytes", "image/png"))],
        )
        assert response.status_code == 200, response.text
        assert len(calls) == 1 and b"xhigh" in calls[0] and b"reference-bytes" in calls[0]
    finally:
        await upstream.aclose()


async def test_registration_is_scoped_repeatable_and_preserves_custom_prices(db_session):
    from app.catalog.gpt_image_26 import register_gpt_image_26

    partner = Partner(
        telegram_id="existing", company_name="Existing", project_name="Existing", balance_rub=Decimal("100")
    )
    unrelated = Model(slug="gpt-image-2", name="Existing model", modality="image", status="production")
    db_session.add_all([partner, unrelated])
    await db_session.flush()
    old_price = PartnerPrice(
        model_id=unrelated.id, price_rub=Decimal("9.99"), provider_cost_usdt=Decimal(".015")
    )
    db_session.add(old_price)
    await db_session.flush()
    await register_gpt_image_26(db_session)
    await db_session.flush()
    models = list(await db_session.scalars(select(Model).where(Model.slug.in_(MODELS))))
    assert len(models) == 2
    assert all(m.status == "draft" and not m.has_successful_smoke for m in models)
    prices = list(await db_session.scalars(select(PartnerPrice).where(PartnerPrice.model_id != unrelated.id)))
    assert len(prices) == 4
    assert all(p.price_rub == Decimal("2.03") and p.provider_cost_usdt == Decimal(".015") for p in prices)
    assert await db_session.scalar(select(func.count()).select_from(ProviderModelCapability)) == 4
    assert await db_session.scalar(select(func.count()).select_from(PartnerPriceSnapshot)) == 4
    custom = await db_session.get(PartnerPriceSnapshot, (partner.id, prices[0].id))
    custom.price_rub, custom.is_custom = Decimal("2.50"), True
    history_count = await db_session.scalar(select(func.count()).select_from(PartnerPriceHistory))
    await register_gpt_image_26(db_session)
    assert await db_session.scalar(select(func.count()).select_from(PartnerPriceHistory)) == history_count
    assert await db_session.scalar(select(func.count()).select_from(ProviderModelCapability)) == 4
    assert custom.price_rub == Decimal("2.50") and custom.is_custom
    assert old_price.price_rub == Decimal("9.99") and unrelated.status == "production"
    assert partner.balance_rub == Decimal("100")


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("result", [{"data": []}, {"data": [{}]}, {"data": [{"b64_json": "invalid"}]}])
async def test_invalid_results_never_debit_the_partner(client, db_session, monkeypatch, model, result):
    partner, headers, upstream = await setup(
        db_session, monkeypatch, lambda req: httpx.Response(200, json=result), model=model, category="image",
        rates=[("default", t, "generation", Decimal("2.03"), Decimal(".015")) for t in ("1K", "2K")],
    )
    try:
        response = await client.post(
            "/v1/images/generations", headers=headers, json={"model": model, "prompt": "Cup"}
        )
        assert response.status_code == 503
        assert partner.balance_rub == Decimal("1000000")
        assert await db_session.scalar(select(func.count()).select_from(LedgerEntry)) == 0
    finally:
        await upstream.aclose()


@pytest.mark.parametrize("model", MODELS)
async def test_url_output_uses_verified_pixels_without_changing_the_delivered_format(
    client, db_session, monkeypatch, model
):
    import base64
    from contextlib import asynccontextmanager

    from app.infrastructure import public_http

    calls = []
    content = base64.b64decode(rendered_image(1440)["b64_json"])

    def image_handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=content, headers={"Content-Type": "image/png"})

    @asynccontextmanager
    async def image_client(**kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(image_handler)) as http:
            yield http

    monkeypatch.setattr(public_http, "public_http_client", image_client)
    _, headers, upstream = await setup(
        db_session, monkeypatch,
        lambda req: httpx.Response(200, json={"data": [{"url": "https://example.org/result.png"}]}),
        model=model, category="image",
        rates=[("default", t, "generation", Decimal("2.03"), Decimal(".015")) for t in ("1K", "2K")],
    )
    try:
        response = await client.post(
            "/v1/images/generations", headers=headers,
            json={"model": model, "prompt": "Cup", "response_format": "url"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"] == [{"url": "https://example.org/result.png"}]
        generation = await db_session.scalar(select(Generation))
        assert generation.usage_snapshot == {"2K": 1}
        assert generation.actual_charge_rub == Decimal("2.03")
        assert calls == ["https://example.org/result.png"]
    finally:
        await upstream.aclose()


async def test_price_preview_is_read_only_and_uses_current_internal_fx(db_session, monkeypatch):
    from app.catalog.gpt_image_26 import price_plan

    async def fx(db):
        return {"rate": Decimal("97.654321")}

    monkeypatch.setattr("app.catalog.gpt_image_26.current_fx", fx)
    plan = await price_plan(db_session)
    assert len(plan) == 4 and all(p.price_rub == Decimal("1.98") for p in plan)
    assert await db_session.scalar(select(func.count()).select_from(Model)) == 0
    assert await db_session.scalar(select(func.count()).select_from(PartnerPrice)) == 0
