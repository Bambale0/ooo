import json
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select
from test_native_inference import setup

from app.billing.models import LedgerEntry
from app.billing.service import release_generation_reserve
from app.generations.models import Generation
from app.generations.service import dispatch_generation_to_provider, poll_generation_provider
from app.inference.accounting import settle_actual
from app.infrastructure.config import get_settings
from app.providers.argolink import ArgoLinkAdapter

BODY = {
    "model": "seedance-2.5",
    "omni_reference_task_type": "edit",
    "prompt": "Make the colors warmer and keep everything else",
    "resolution": "720p",
    "reference_videos": [{"url": "https://example.org/street.mp4"}],
}
RATES = [
    ("default", "720p", "second", Decimal("23.80"), Decimal(".17")),
    ("edit", "720p", "second", Decimal("22.10"), Decimal(".196")),
]


@pytest.mark.parametrize("late_success", [False, True])
async def test_edit_http_quote_and_settlement_are_frozen_and_idempotent(client, db_session, monkeypatch, late_success):
    submitted = []

    def handler(request):
        if request.method == "POST":
            submitted.append(json.loads(request.content))
            return httpx.Response(202, json={"request_id": "edit-provider-task"})
        return httpx.Response(200, json={
            "status": "done",
            "usage": {"output_seconds": 10, "reference_video_seconds": 10, "billed_seconds": 20},
        })

    settings = get_settings()
    monkeypatch.setattr(settings, "seedance_25_edit_markup_rub_per_second", Decimal("2.50"))
    monkeypatch.setattr(settings, "rub_per_usdt", Decimal("100"))
    partner, headers, upstream = await setup(
        db_session, monkeypatch, handler, model="seedance-2.5", category="video", rates=RATES
    )
    monkeypatch.setattr(
        "app.generations.service.get_partner_provider_adapter",
        AsyncMock(return_value=ArgoLinkAdapter(api_key="upstream", client=upstream)),
    )
    try:
        response = await client.post("/v1/videos/generations", headers=headers, json=BODY)
        assert response.status_code == 202, response.text
        generation = await db_session.get(Generation, response.json()["request_id"])
        assert generation.partner_price_rub == Decimal("1326.00")
        accepted = generation.request_payload["rates"]["seconds"]
        assert Decimal(accepted["retail"]) == Decimal("22.10")
        assert Decimal(accepted["cost"]) == Decimal(".196")
        assert generation.request_payload["pricing_policy"] == {
            "mode": "edit", "type": "cost_plus", "markup_rub_per_second": "2.50"
        }
        original_rates = dict(accepted)
        monkeypatch.setattr(settings, "seedance_25_edit_markup_rub_per_second", Decimal("9.50"))
        monkeypatch.setattr(settings, "rub_per_usdt", Decimal("120"))
        duplicate = await client.post("/v1/videos/generations", headers=headers, json=BODY)
        assert duplicate.status_code == 202 and duplicate.json()["request_id"] == generation.id
        assert generation.request_payload["rates"]["seconds"] == original_rates
        attempt = await dispatch_generation_to_provider(db_session, generation)
        attempt.next_poll_at = None
        if late_success:
            await release_generation_reserve(db_session, generation, reason="Test provisional timeout refund")
            generation.status = attempt.status = "timeout"
        await db_session.commit()
        await poll_generation_provider(db_session, generation)
        assert submitted == [BODY]
        assert generation.status == "completed"
        assert generation.actual_charge_rub == Decimal("442.00")
        assert generation.actual_provider_cost_usdt == Decimal("3.92")
        assert generation.usage_snapshot == {"seconds": 20}
        await db_session.refresh(partner)
        assert partner.balance_rub == Decimal("999558.00")
        entries = list((await db_session.scalars(select(LedgerEntry))).all())
        assert sum((x.amount_rub for x in entries), Decimal(0)) == Decimal("-442.00")
        await settle_actual(db_session, generation, {"seconds": 20})
        assert len(list((await db_session.scalars(select(LedgerEntry))).all())) == len(entries)
    finally:
        await upstream.aclose()


async def test_catalog_edit_price_matches_admission_without_changing_default(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "seedance_25_edit_markup_rub_per_second", Decimal("2.50"))
    monkeypatch.setattr(get_settings(), "rub_per_usdt", Decimal("100"))
    _, _, upstream = await setup(
        db_session, monkeypatch, lambda request: httpx.Response(500),
        model="seedance-2.5", category="video", rates=RATES,
    )
    try:
        response = await client.get("/api/v1/catalog/pricing")
        assert response.status_code == 200
        amounts = {r["mode"]: Decimal(r["price_rub"]) for r in response.json()}
        assert amounts == {"default": Decimal("23.80"), "edit": Decimal("22.10")}
    finally:
        await upstream.aclose()
