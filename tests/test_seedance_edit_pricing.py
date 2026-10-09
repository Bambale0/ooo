import copy
import json
from decimal import ROUND_HALF_UP, Decimal

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from test_native_inference import setup

from app.billing.models import LedgerEntry
from app.catalog.models import PartnerPrice
from app.contracts.registry import validate_request
from app.generations.models import Generation
from app.generations.service import dispatch_generation_to_provider, poll_generation_provider
from app.inference.accounting import settle_actual
from app.inference.service import quote
from app.infrastructure.config import Settings, get_settings
from app.providers.argolink import ArgoLinkAdapter

FX = Decimal("83.296218")
MARKUP = Decimal("2.50")
COSTS = {"480p": Decimal(".0874"), "720p": Decimal(".196"), "1080p": Decimal(".483")}
DEFAULTS = {
    "480p": (Decimal("12.00"), Decimal(".078")),
    "720p": (Decimal("23.80"), Decimal(".170")),
    "1080p": (Decimal("57.13"), Decimal(".430")),
}


def price_table():
    return [
        ("default", tier, "second", retail, cost) for tier, (retail, cost) in DEFAULTS.items()
    ] + [("edit", tier, "second", Decimal("99"), cost) for tier, cost in COSTS.items()]


def prices():
    return [
        PartnerPrice(mode=mode, resolution=tier, billing_unit=unit, price_rub=retail, provider_cost_usdt=cost)
        for mode, tier, unit, retail, cost in price_table()
    ]


def edit_body(resolution="720p"):
    return {
        "model": "seedance-2.5",
        "prompt": "Make the colors in @Video 1 warmer",
        "omni_reference_task_type": "edit",
        "resolution": resolution,
        "reference_videos": [{"url": "https://example.org/source.mp4"}],
    }


def money(amount):
    return amount.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)


@pytest.fixture
def edit_pricing(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "seedance_25_edit_markup_rub_per_second", MARKUP)
    monkeypatch.setattr(settings, "rub_per_usdt", FX)
    return settings


@pytest.mark.parametrize("resolution", list(COSTS))
@pytest.mark.parametrize("field", ["reference_videos", "video_urls", "input_references"])
def test_only_explicit_edit_uses_precise_cost_plus_rate(edit_pricing, resolution, field):
    body = edit_body(resolution)
    if field != "reference_videos":
        body.pop("reference_videos")
        body[field] = (
            ["https://example.org/source.mp4"] if field == "video_urls"
            else [{"type": "video_url", "video_url": {"url": "https://example.org/source.mp4"}}]
        )
    original = copy.deepcopy(body)
    validate_request("videos/generations", body)
    rates, units, tier = quote("videos/generations", body, prices(), fx=FX)
    assert body == original
    assert tier == resolution and units == {"seconds": 60}
    assert Decimal(rates["seconds"]["retail"]) == COSTS[resolution] * FX + MARKUP
    assert Decimal(rates["seconds"]["cost"]) == COSTS[resolution]


@pytest.mark.parametrize("task_type", [None, "auto", "reference"])
@pytest.mark.parametrize("has_video", [False, True])
@pytest.mark.parametrize("resolution", list(COSTS))
def test_reference_and_non_video_requests_keep_previous_rates(edit_pricing, task_type, has_video, resolution):
    body = {"model": "seedance-2.5", "prompt": "Animate", "duration": 10, "resolution": resolution}
    if task_type is not None:
        body["omni_reference_task_type"] = task_type
    if has_video:
        body["reference_videos"] = [{"url": "https://example.org/source.mp4"}]
    rates, units, _ = quote("videos/generations", body, prices(), fx=FX)
    assert Decimal(rates["seconds"]["retail"]) == DEFAULTS[resolution][0]
    assert Decimal(rates["seconds"]["cost"]) == DEFAULTS[resolution][1]
    assert units == {"seconds": 40 if has_video else 10}


def test_other_seedance_25_variant_is_not_repriced(edit_pricing):
    body = {**edit_body(), "model": "seedance-2.5-self-developed-nsfw"}
    rates, _, _ = quote("videos/generations", body, prices(), fx=FX)
    assert Decimal(rates["seconds"]["retail"]) == DEFAULTS["720p"][0]


def test_unconfigured_rule_preserves_old_edit_pricing(edit_pricing, monkeypatch):
    monkeypatch.setattr(edit_pricing, "seedance_25_edit_markup_rub_per_second", None)
    rates, _, _ = quote("videos/generations", edit_body(), prices(), fx=FX)
    assert Decimal(rates["seconds"]["retail"]) == DEFAULTS["720p"][0]


def test_enabled_edit_never_falls_back_to_default_procurement(edit_pricing):
    with pytest.raises(HTTPException) as failure:
        quote("videos/generations", edit_body(), [p for p in prices() if p.mode == "default"], fx=FX)
    assert failure.value.status_code == 503


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity"])
def test_markup_rejects_invalid_configuration(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seedance_25_edit_markup_rub_per_second=value)


@pytest.mark.parametrize("failed", [False, True])
async def test_edit_http_settlement_and_refund_are_idempotent(
    client, db_session, monkeypatch, edit_pricing, failed
):
    submitted = []

    def handler(request):
        if request.method == "POST":
            submitted.append(json.loads(request.content))
            return httpx.Response(202, json={"request_id": "private-edit-task"})
        if failed:
            return httpx.Response(200, json={"status": "failed", "error": {"message": "Generation failed"}})
        return httpx.Response(
            200,
            json={
                "status": "done",
                "usage": {"output_seconds": 6, "reference_video_seconds": 6, "billed_seconds": 12},
            },
        )

    partner, headers, upstream = await setup(
        db_session, monkeypatch, handler, model="seedance-2.5", category="video", rates=price_table()
    )
    adapter = ArgoLinkAdapter(api_key="upstream", client=upstream)

    async def get_adapter(*args, **kwargs):
        return adapter

    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", get_adapter)
    try:
        body = edit_body()
        response = await client.post("/v1/videos/generations", headers=headers, json=body)
        assert response.status_code == 202, response.text
        generation = await db_session.get(Generation, response.json()["request_id"])
        rate = COSTS["720p"] * FX + MARKUP
        reserve = money(rate * 60)
        assert generation.partner_price_rub == reserve
        assert generation.request_payload["rates"]["seconds"] == {
            "retail": str(rate), "cost": str(Decimal(".196000")),
        }
        accepted_rates = copy.deepcopy(generation.request_payload["rates"])
        await db_session.refresh(partner)
        assert partner.balance_rub == Decimal("1000000") - reserve

        # Every mutable pricing source changes after acceptance. The accepted
        # rates and original idempotency key must still control the final bill.
        monkeypatch.setattr(edit_pricing, "rub_per_usdt", Decimal("100"))
        monkeypatch.setattr(edit_pricing, "seedance_25_edit_markup_rub_per_second", Decimal("9"))
        row = (await db_session.execute(select(PartnerPrice).where(
            PartnerPrice.mode == "edit", PartnerPrice.resolution == "720p"
        ))).scalar_one()
        row.provider_cost_usdt = Decimal(".25")
        await db_session.commit()
        duplicate = await client.post("/v1/videos/generations", headers=headers, json=body)
        assert duplicate.status_code == 202 and duplicate.json()["request_id"] == generation.id
        attempt = await dispatch_generation_to_provider(db_session, generation)
        attempt.next_poll_at = None
        await db_session.commit()
        await poll_generation_provider(db_session, generation)
        await db_session.commit()
        assert submitted == [body]
        assert generation.request_payload["rates"] == accepted_rates
        charged = Decimal("0") if failed else money(rate * 12)
        assert generation.status == ("failed" if failed else "completed")
        if not failed:
            assert generation.actual_charge_rub == charged
            assert generation.actual_provider_cost_usdt == COSTS["720p"] * 12
            assert generation.usage_snapshot == {"seconds": 12}
            await settle_actual(db_session, generation, {"seconds": 12})
        await poll_generation_provider(db_session, generation)
        await db_session.refresh(partner)
        assert partner.balance_rub == Decimal("1000000") - charged
        ledger = list((await db_session.scalars(select(LedgerEntry))).all())
        assert len(ledger) == 2
        assert sum((row.amount_rub for row in ledger), Decimal("0")) == -charged
        status = await client.get(f"/v1/videos/{generation.id}", headers=headers)
        assert status.json()["status"] == ("failed" if failed else "done")
    finally:
        await upstream.aclose()


async def test_previously_accepted_edit_keeps_old_bill(client, db_session, monkeypatch, edit_pricing):
    def handler(request):
        raise AssertionError("This test settles a saved quote without submitting a provider job")

    partner, headers, upstream = await setup(
        db_session, monkeypatch, handler, model="seedance-2.5", category="video", rates=price_table()
    )
    try:
        monkeypatch.setattr(edit_pricing, "seedance_25_edit_markup_rub_per_second", None)
        response = await client.post("/v1/videos/generations", headers=headers, json=edit_body())
        assert response.status_code == 202
        generation = await db_session.get(Generation, response.json()["request_id"])
        assert generation.partner_price_rub == Decimal("1428.00")
        monkeypatch.setattr(edit_pricing, "seedance_25_edit_markup_rub_per_second", MARKUP)
        await settle_actual(db_session, generation, {"seconds": 12})
        await settle_actual(db_session, generation, {"seconds": 12})
        assert generation.actual_charge_rub == Decimal("285.60")
        await db_session.refresh(partner)
        assert partner.balance_rub == Decimal("999714.40")
    finally:
        await upstream.aclose()


async def test_catalog_shows_edit_rate_without_repricing_reference(
    client, db_session, monkeypatch, edit_pricing
):
    def handler(request):
        raise AssertionError("Reading the public price list must not contact a provider")

    _, _, upstream = await setup(
        db_session, monkeypatch, handler, model="seedance-2.5", category="video", rates=price_table()
    )
    try:
        response = await client.get("/api/v1/catalog/pricing")
        assert response.status_code == 200
        rows = response.json()
        assert len(rows) == 6
        for row in rows:
            tier = row["resolution"]
            expected = COSTS[tier] * FX + MARKUP if row["mode"] == "edit" else DEFAULTS[tier][0]
            assert Decimal(row["price_rub"]) == expected
            assert "provider_cost_usdt" not in row
        monkeypatch.setattr(edit_pricing, "seedance_25_edit_markup_rub_per_second", None)
        disabled = await client.get("/api/v1/catalog/pricing")
        assert {row["mode"] for row in disabled.json()} == {"default"}
    finally:
        await upstream.aclose()
