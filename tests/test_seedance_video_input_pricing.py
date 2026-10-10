"""Video-input tariff is independent of provider task mode (reference/edit)."""

from decimal import Decimal

import httpx
import pytest
from test_native_inference import setup
from test_seedance_edit_pricing import COSTS, FX, prices

from app.catalog.video_edit_pricing import video_pricing_mode
from app.inference.accounting import charges
from app.inference.service import quote
from app.infrastructure.config import get_settings

VIDEO_COSTS = {"480p": Decimal("0.0523"), "720p": Decimal("0.117"), "1080p": Decimal("0.289")}


@pytest.fixture
def edit_rule(monkeypatch):
    monkeypatch.setattr(get_settings(), "seedance_25_edit_markup_rub_per_second", Decimal("2.50"))


def request(tier="720p", *, video=True, task_mode="reference", image=True):
    body = {
        "model": "seedance-2.5",
        "prompt": "Keep outfit from @Image 1 and motion from @Video 1",
        "resolution": tier,
        "duration": 11,
        "aspect_ratio": "9:16",
    }
    if video:
        body["reference_videos"] = [{"url": "https://example.org/source.mp4"}]
    if image:
        body["reference_images"] = [{"url": "https://example.org/outfit.jpg"}]
    if task_mode:
        body["omni_reference_task_type"] = task_mode
    return body


@pytest.mark.parametrize("tier", VIDEO_COSTS)
@pytest.mark.parametrize("mode", ["auto", "reference", None])
def test_any_seedance_25_video_reference_uses_edit_retail_and_video_input_procurement(edit_rule, tier, mode):
    body = request(tier, task_mode=mode)
    original = dict(body)
    rates, reserved, resolution = quote("videos/generations", body, prices(), fx=FX)
    assert video_pricing_mode(body) == "edit"
    assert rates["seconds"]["retail"] == str(COSTS[tier] * FX + Decimal("2.50"))
    assert Decimal(rates["seconds"]["cost"]) == VIDEO_COSTS[tier]
    assert reserved == {"seconds": 41}
    assert resolution == tier
    assert body == original
    actual, provider_estimate = charges(rates, {"seconds": 21})
    assert actual == (21 * (COSTS[tier] * FX + Decimal("2.50"))).quantize(Decimal(".01"))
    assert provider_estimate == 21 * VIDEO_COSTS[tier]


def test_edit_single_video_uses_same_video_input_procurement(edit_rule):
    body = {
        "model": "seedance-2.5",
        "prompt": "Change colors",
        "resolution": "720p",
        "omni_reference_task_type": "edit",
        "reference_videos": [{"url": "https://example.org/source.mp4"}],
    }
    rates, reserved, _ = quote("videos/generations", body, prices(), fx=FX)
    assert Decimal(rates["seconds"]["cost"]) == VIDEO_COSTS["720p"]
    assert reserved == {"seconds": 60}


@pytest.mark.parametrize("mode", [None, "auto", "reference"])
def test_no_video_preserves_existing_default_tariff_and_procurement(edit_rule, mode):
    body = request(video=False, task_mode=mode)
    rates, units, _ = quote("videos/generations", body, prices(), fx=FX)
    assert video_pricing_mode(body) == "default"
    assert Decimal(rates["seconds"]["retail"]) == Decimal("23.80")
    assert Decimal(rates["seconds"]["cost"]) == COSTS["720p"]
    assert units == {"seconds": 11}


def test_edit_mode_label_alone_does_not_trigger_video_input_tariff(edit_rule):
    """Only an actual video reference selects the edit price, not a mode label."""
    body = request(video=False, task_mode="edit")
    assert video_pricing_mode(body) == "default"
    rates, units, _ = quote("videos/generations", body, prices(), fx=FX)
    assert Decimal(rates["seconds"]["retail"]) == Decimal("23.80")
    assert Decimal(rates["seconds"]["cost"]) == COSTS["720p"]
    assert units["seconds"] > 0


def test_other_seedance_family_stays_default(edit_rule):
    body = request()
    body["model"] = "seedance-2.0"
    assert video_pricing_mode(body) == "default"


async def test_public_price_explains_video_input_rule_and_matches_catalog(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "seedance_25_edit_markup_rub_per_second", Decimal("2.50"))
    monkeypatch.setattr(get_settings(), "rub_per_usdt", Decimal("100"))
    _, _, upstream = await setup(
        db_session,
        monkeypatch,
        lambda req: httpx.Response(500),
        model="seedance-2.5",
        category="video",
        rates=[
            ("default", "720p", "second", Decimal("23.80"), Decimal(".196")),
            ("edit", "720p", "second", Decimal("18.83"), Decimal(".196")),
        ],
    )
    try:
        catalog = (await client.get("/api/v1/catalog/pricing")).json()
        edit = next(r for r in catalog if r["mode"] == "edit")
        website = (await client.get("/price")).text
        assert Decimal(edit["price_rub"]) == Decimal("22.10")
        assert "22.10" in website
        assert "с видеореференсом" in website
        assert "reference" in website
        assert "оплачиваем" in website.lower()
    finally:
        await upstream.aclose()


async def test_mixed_reference_full_admission_usage_settlement_and_idempotency(client, db_session, monkeypatch):
    """A photo must not force the expensive no-video retail or mutate provider request."""
    import json
    from unittest.mock import AsyncMock

    from sqlalchemy import select

    from app.billing.models import LedgerEntry
    from app.generations.models import Generation
    from app.generations.service import dispatch_generation_to_provider, poll_generation_provider
    from app.providers.argolink import ArgoLinkAdapter

    provider_requests = []

    def handler(req):
        if req.method == "POST":
            provider_requests.append(json.loads(req.content))
            return httpx.Response(202, json={"request_id": "mixed-media-provider"})
        return httpx.Response(
            200,
            json={
                "status": "done",
                "usage": {"output_seconds": 11, "reference_video_seconds": 10, "billed_seconds": 21},
            },
        )

    monkeypatch.setattr(get_settings(), "seedance_25_edit_markup_rub_per_second", Decimal("2.50"))
    monkeypatch.setattr(get_settings(), "rub_per_usdt", Decimal("100"))
    owner, headers, upstream = await setup(
        db_session,
        monkeypatch,
        handler,
        model="seedance-2.5",
        category="video",
        rates=[
            ("default", "720p", "second", Decimal("23.80"), Decimal(".196")),
            ("edit", "720p", "second", Decimal("18.83"), Decimal(".196")),
        ],
    )
    monkeypatch.setattr(
        "app.generations.service.get_partner_provider_adapter",
        AsyncMock(return_value=ArgoLinkAdapter(api_key="test", client=upstream)),
    )
    original = request()
    try:
        accepted = await client.post("/v1/videos/generations", headers=headers, json=original)
        assert accepted.status_code == 202, accepted.text
        job_id = accepted.json()["request_id"]
        generation = await db_session.get(Generation, job_id)
        assert generation.request_payload["native_body"] == original
        assert generation.request_payload["pricing_policy"]["mode"] == "edit"
        assert generation.partner_price_rub == Decimal("906.10")
        assert Decimal(generation.request_payload["rates"]["seconds"]["cost"]) == Decimal(".117")

        duplicate = await client.post("/v1/videos/generations", headers=headers, json=original)
        assert duplicate.status_code == 202 and duplicate.json()["request_id"] == job_id

        attempt = await dispatch_generation_to_provider(db_session, generation)
        attempt.next_poll_at = None
        await db_session.commit()
        await poll_generation_provider(db_session, generation)

        assert len(provider_requests) == 1
        assert provider_requests[0] == original
        assert generation.status == "completed"
        assert generation.actual_charge_rub == Decimal("464.10")
        assert generation.actual_provider_cost_usdt == Decimal("2.457")
        await db_session.refresh(owner)
        assert owner.balance_rub == Decimal("999535.90")
        rows = list((await db_session.scalars(select(LedgerEntry).where(LedgerEntry.generation_id == job_id))).all())
        assert len(rows) == 2
        assert sum((r.amount_rub for r in rows), Decimal(0)) == Decimal("-464.10")
    finally:
        await upstream.aclose()
