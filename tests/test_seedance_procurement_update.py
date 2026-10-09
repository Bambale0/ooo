from decimal import Decimal

import pytest
from sqlalchemy import select
from test_infai_fallback import seed

from app.catalog.models import PartnerPrice
from app.catalog.sync import variants
from app.contracts.registry import MODELS
from app.generations.service import poll_generation_provider
from app.providers.base import ProviderPollResult


def test_reviewed_seedance25_procurement_matches_supplier_notice():
    rates = {resolution: cost for _, resolution, _, cost in variants(MODELS["seedance-2.5"])}
    assert rates == {"480p": Decimal(".0874"), "720p": Decimal(".196"), "1080p": Decimal(".483")}


async def test_reviewed_rates_compare_equal_to_numeric_live_catalog(monkeypatch):
    import httpx

    from app.catalog.sync import check_catalog_drift

    def handler(request):
        return httpx.Response(
            200,
            content=(
                b'{"revision":"reviewed","items":[{"id":"seedance-2.5","category":"video",'
                b'"endpoint":"/v1/videos/generations","pricing":{"effective":'
                b'{"currency":"USD","billing_mode":"video","unit":"second","generation_per_unit":0.0874,'
                b'"tiers":[{"label":"480p","price":0.0874},{"label":"720p","price":0.196},'
                b'{"label":"1080p","price":0.483}]}}}]}'
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://argolink.io") as client:
        monkeypatch.setattr("app.catalog.sync.get_provider_http_client", lambda provider: client)
        assert "seedance-2.5" not in (await check_catalog_drift())["changed"]


@pytest.mark.parametrize("reported", [None, "3.45", "0"])
async def test_primary_success_records_attempt_cost_without_repricing(db_session, monkeypatch, reported):
    partner, generation, primary, _ = await seed(db_session)
    usage = {"billed_seconds": 15}
    if reported is not None:
        usage["provider_charge_usdt"] = reported

    class Successful:
        async def poll_generation(self, task_id):
            return ProviderPollResult(status="completed", usage=usage)

    async def adapter(*args, **kwargs):
        return Successful()

    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", adapter)
    await poll_generation_provider(db_session, generation, "argolink")
    cost = Decimal(reported) if reported is not None else Decimal("2.499990")
    assert primary.provider_cost_usdt == cost
    assert primary.cost_status == ("reported" if reported is not None else "estimated")
    assert primary.usage_snapshot == usage
    assert generation.actual_provider_cost_usdt == cost
    assert generation.actual_charge_rub == Decimal("327")
    assert partner.balance_rub == Decimal("673")
    await poll_generation_provider(db_session, generation, "argolink")
    assert partner.balance_rub == Decimal("673")


@pytest.mark.parametrize("reported", [True, "NaN", "-1", "broken", None, 3.45, "1e30"])
async def test_invalid_reported_success_cost_requires_reconciliation(db_session, monkeypatch, reported):
    _, generation, primary, _ = await seed(db_session)

    class Successful:
        async def poll_generation(self, task_id):
            return ProviderPollResult(
                status="completed", usage={"billed_seconds": 15, "provider_charge_usdt": reported}
            )

    async def adapter(*args, **kwargs):
        return Successful()

    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", adapter)
    await poll_generation_provider(db_session, generation, "argolink")
    assert generation.status == primary.status == "reconciliation_required"
    assert generation.actual_charge_rub is None


async def test_procurement_update_preserves_partner_and_generation_snapshots(db_session):
    from app.catalog.reviewed_updates import apply_procurement_update

    _, generation, _, _ = await seed(db_session)
    price = PartnerPrice(
        model_id=generation.model_id,
        mode="default",
        resolution="720p",
        price_rub=Decimal("23.80"),
        provider_cost_usdt=Decimal(".170"),
        billing_unit="second",
    )
    db_session.add(price)
    await db_session.flush()
    payload = dict(generation.request_payload)
    kwargs = dict(
        model_id=generation.model_id,
        expected_costs={"720p": Decimal(".170")},
        new_costs={"720p": Decimal(".196")},
        source="supplier-notice",
        apply=True,
    )
    result = await apply_procurement_update(db_session, **kwargs)
    assert result["updated"] == ["720p"]
    assert price.provider_cost_usdt == Decimal(".196")
    assert price.price_rub == Decimal("23.80")
    assert generation.partner_price_rub == Decimal("327")
    assert generation.provider_cost_usdt_snapshot == Decimal("2.5")
    assert generation.request_payload == payload
    assert (await apply_procurement_update(db_session, **kwargs))["updated"] == []
    assert len(list(await db_session.scalars(select(PartnerPrice)))) == 1


async def test_reported_debit_keeps_decimal_precision():
    import httpx

    from app.providers.argolink import ArgoLinkAdapter

    def handler(request):
        return httpx.Response(
            200,
            content=(b'{"status":"done","usage":{"billed_seconds":15,"provider_charge_usdt":3.450000000000000001}}'),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://argolink.io") as client:
        result = await ArgoLinkAdapter(api_key="test", client=client).poll_generation("task")
    assert result.usage["provider_charge_usdt"] == "3.450000000000000001"


async def test_estimated_attempt_can_be_reconciled_without_changing_retail(client, db_session, admin_headers):
    from app.billing.models import CoverageLedgerEntry
    from app.inference.accounting import settle_actual

    partner, generation, primary, _ = await seed(db_session)
    generation.status = primary.status = "completed"
    await settle_actual(db_session, generation, {"seconds": 15})
    primary.cost_status = "estimated"
    primary.provider_cost_usdt = generation.actual_provider_cost_usdt
    await db_session.commit()
    pending = await client.get("/api/v1/providers/reconciliation", headers=admin_headers)
    assert any(item["id"] == generation.id for item in pending.json())
    url = f"/api/v1/providers/reconciliation/{generation.id}/attempts/{primary.id}/cost"
    payload = {"outcome": "charged", "provider_cost_usdt": "3.45", "reason": "Verified supplier debit history"}
    response = await client.post(url, headers=admin_headers, json=payload)
    assert response.status_code == 200, response.text
    await db_session.refresh(generation)
    await db_session.refresh(partner)
    assert generation.actual_provider_cost_usdt == Decimal("3.45")

    assert generation.actual_charge_rub == Decimal("327")
    assert partner.balance_rub == Decimal("673")
    entries = list(
        await db_session.scalars(select(CoverageLedgerEntry).where(CoverageLedgerEntry.generation_id == generation.id))
    )
    assert sum((entry.amount_rub for entry in entries), Decimal(0)) == Decimal("-293.25")
    assert (await client.post(url, headers=admin_headers, json=payload)).status_code == 200
    await db_session.refresh(generation)
    assert generation.actual_provider_cost_usdt == Decimal("3.45")


async def test_procurement_dry_run_and_conflict_are_non_mutating(db_session):
    from fastapi import HTTPException

    from app.catalog.models import PartnerPriceHistory
    from app.catalog.reviewed_updates import apply_procurement_update

    _, generation, _, _ = await seed(db_session)
    price = PartnerPrice(
        model_id=generation.model_id,
        mode="default",
        resolution="720p",
        price_rub=Decimal("23.80"),
        provider_cost_usdt=Decimal(".170"),
        billing_unit="second",
    )
    db_session.add(price)
    await db_session.flush()
    kwargs = dict(
        model_id=generation.model_id,
        expected_costs={"720p": Decimal(".170")},
        new_costs={"720p": Decimal(".196")},
        source="supplier",
    )
    assert (await apply_procurement_update(db_session, **kwargs))["pending"] == ["720p"]
    assert price.provider_cost_usdt == Decimal(".170")
    assert list(await db_session.scalars(select(PartnerPriceHistory))) == []
    price.provider_cost_usdt = Decimal(".250")
    with pytest.raises(HTTPException) as conflict:
        await apply_procurement_update(db_session, **kwargs, apply=True)
    assert conflict.value.status_code == 409
    assert price.provider_cost_usdt == Decimal(".250")


async def test_backfill_enriches_only_single_primary_attempts(db_session):
    from app.billing.provider_credits import backfill_primary_cost_estimates
    from app.providers.models import ProviderAttempt

    _, generation, primary, credential = await seed(db_session)
    generation.status = primary.status = "completed"
    generation.actual_provider_cost_usdt = Decimal("2.5")
    generation.usage_snapshot = {"seconds": 15}
    fallback = ProviderAttempt(
        generation_id=generation.id,
        provider="infai",
        credential_id=credential.id,
        status="failed",
        cost_status="unknown",
    )
    db_session.add(fallback)
    await db_session.flush()
    assert await backfill_primary_cost_estimates(db_session) == 0

    await db_session.delete(fallback)
    await db_session.flush()
    assert await backfill_primary_cost_estimates(db_session) == 1
    assert primary.cost_status == "estimated"
    assert primary.provider_cost_usdt == Decimal("2.5")
    assert primary.usage_snapshot == {"seconds": 15}
    assert generation.partner_price_rub == Decimal("327")
    assert generation.actual_provider_cost_usdt == Decimal("2.5")
    assert await backfill_primary_cost_estimates(db_session) == 0


async def test_attempt_reconciliation_keeps_other_unknown_cost_hold(client, db_session, admin_headers):
    from app.billing.models import CoverageLedgerEntry
    from app.providers.models import ProviderAttempt

    _, generation, primary, credential = await seed(db_session)
    generation.status = "completed"
    generation.actual_provider_cost_usdt = Decimal("2")
    generation.provider_cost_hold_usdt = Decimal("5")
    generation.provider_cost_hold_rub = Decimal("425")
    primary.status = "failed"
    primary.cost_status = "unknown"
    primary.cost_reserve_usdt = Decimal("2.5")
    db_session.add_all(
        [
            ProviderAttempt(
                generation_id=generation.id,
                provider="infai",
                credential_id=credential.id,
                status="failed",
                cost_status="unknown",
                cost_reserve_usdt=Decimal("2.5"),
            ),
            ProviderAttempt(
                generation_id=generation.id,
                provider="asale",
                status="completed",
                cost_status="settled",
                provider_cost_usdt=Decimal("2"),
            ),
        ]
    )
    await db_session.commit()
    url = f"/api/v1/providers/reconciliation/{generation.id}/attempts/{primary.id}/cost"
    response = await client.post(
        url, headers=admin_headers, json={"outcome": "free", "reason": "Supplier confirmed this attempt free"}
    )
    assert response.status_code == 200, response.text
    await db_session.refresh(generation)
    assert generation.provider_cost_hold_usdt == Decimal("2.5")
    assert generation.provider_cost_hold_rub == Decimal("212.50")
    entries = list(
        await db_session.scalars(select(CoverageLedgerEntry).where(CoverageLedgerEntry.generation_id == generation.id))
    )
    assert sum((entry.amount_rub for entry in entries), Decimal(0)) == Decimal("-382.50")
