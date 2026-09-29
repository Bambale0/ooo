from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.catalog.models import Model, PartnerPrice
from app.catalog.schemas import PartnerPriceUpsert
from app.catalog.sync import variants
from app.contracts.registry import MODELS


def free_price(**overrides):
    return {
        "model_slug": "glm-5.3-flash",
        "mode": "cache_write_tokens",
        "resolution": "default",
        "billing_unit": "million_tokens",
        "price_rub": "0",
        "provider_cost_usdt": "0",
        **overrides,
    }


@pytest.mark.parametrize("retail", ["0", "3.50"])
def test_documented_free_token_procurement_accepts_zero_cost(retail):
    payload = PartnerPriceUpsert(**free_price(price_rub=retail))
    assert (payload.price_rub, payload.provider_cost_usdt) == (Decimal(retail), Decimal(0))


@pytest.mark.parametrize(
    "overrides",
    [
        {"model_slug": "unknown-model"},
        {"model_slug": "gpt-5.6-luna"},
        {"model_slug": "gpt-5.4"},  # Missing procurement field must not imply free.
        {"model_slug": "nano-banana-pro"},
        {"mode": "input_tokens"},
        {"mode": "output_tokens"},
        {"mode": "cached_input_tokens"},
        {"mode": "cache_write_1h_tokens"},
        {"resolution": "1K"},
        {"billing_unit": "generation"},
        {"billing_unit": "second"},
    ],
)
@pytest.mark.parametrize("zero_field", ["price_rub", "provider_cost_usdt"])
def test_zero_rates_require_exact_reviewed_free_token_variant(overrides, zero_field):
    payload = free_price(price_rub="10", provider_cost_usdt="0.01", **overrides)
    payload[zero_field] = "0"
    with pytest.raises(ValidationError):
        PartnerPriceUpsert(**payload)


@pytest.mark.parametrize("field", ["price_rub", "provider_cost_usdt"])
@pytest.mark.parametrize("value", ["-0.01", "NaN", "Infinity", "-Infinity"])
def test_free_token_price_still_rejects_negative_or_nonfinite_amounts(field, value):
    with pytest.raises(ValidationError):
        PartnerPriceUpsert(**free_price(**{field: value}))


async def test_zero_cache_write_can_be_saved_and_model_enabled(client, db_session, admin_headers):
    model = Model(
        slug="glm-5.3-flash",
        name="GLM 5.3 Flash",
        modality="llm",
        status="draft",
        has_provider_integration=True,
        has_public_docs=True,
        has_successful_smoke=True,
    )
    db_session.add(model)
    await db_session.flush()
    for mode, resolution, unit, cost in variants(MODELS[model.slug]):
        result = await client.put(
            "/api/v1/catalog/pricing",
            headers=admin_headers,
            json=free_price(
                mode=mode,
                resolution=resolution,
                billing_unit=unit,
                price_rub=str(cost * Decimal("110")),
                provider_cost_usdt=str(cost),
            ),
        )
        assert result.status_code == 204, result.text
    saved = (
        await db_session.execute(
            select(PartnerPrice).where(PartnerPrice.model_id == model.id, PartnerPrice.mode == "cache_write_tokens")
        )
    ).scalar_one()
    assert (saved.price_rub, saved.provider_cost_usdt) == (Decimal(0), Decimal(0))
    result = await client.post(f"/api/v1/catalog/models/{model.slug}/enable", headers=admin_headers)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "production"


@pytest.mark.parametrize(
    "overrides",
    [
        {"mode": "input_tokens", "price_rub": Decimal("0"), "provider_cost_usdt": Decimal("0")},
        {"mode": "cache_write_tokens", "price_rub": Decimal("-1"), "provider_cost_usdt": Decimal("0")},
        {"mode": "cache_write_tokens", "price_rub": Decimal("0"), "provider_cost_usdt": Decimal("-1")},
        {"mode": "cache_write_tokens", "resolution": "1K"},
        {"mode": "cache_write_tokens", "billing_unit": "generation"},
    ],
)
async def test_enable_rechecks_invalid_persisted_zero_or_negative_prices(client, db_session, admin_headers, overrides):
    model = Model(
        slug="glm-5.3-flash",
        name="GLM 5.3 Flash",
        modality="llm",
        status="draft",
        has_provider_integration=True,
        has_public_docs=True,
        has_successful_smoke=True,
    )
    db_session.add(model)
    await db_session.flush()
    for mode, resolution, unit, cost in variants(MODELS[model.slug]):
        values = {
            "model_id": model.id,
            "mode": mode,
            "resolution": resolution,
            "billing_unit": unit,
            "price_rub": cost * Decimal("110"),
            "provider_cost_usdt": cost,
        }
        if mode == overrides["mode"]:
            values.update(overrides)
        db_session.add(PartnerPrice(**values))
    await db_session.commit()
    result = await client.post(f"/api/v1/catalog/models/{model.slug}/enable", headers=admin_headers)
    assert (result.status_code, result.json()["detail"]) == (409, "economic_gate_missing")


async def test_free_cache_rate_recovers_false_margin_alarm_but_paid_and_unreviewed_rates_still_alert(
    db_session, monkeypatch
):
    from unittest.mock import AsyncMock

    from app.billing.incidents import financial_tick
    from app.billing.models import FinancialIncident

    monkeypatch.setattr("app.billing.incidents.capital_state", AsyncMock(return_value={"safe": None}))
    monkeypatch.setattr("app.billing.incidents.current_fx", AsyncMock(return_value={"rate": Decimal("100")}))
    notify = AsyncMock()
    monkeypatch.setattr("app.billing.incidents.notify", notify)
    model = Model(slug="glm-5.3-flash", name="GLM 5.3 Flash", modality="llm", status="production")
    db_session.add(model)
    await db_session.flush()
    free = PartnerPrice(
        model_id=model.id,
        mode="cache_write_tokens",
        resolution="default",
        billing_unit="million_tokens",
        price_rub=Decimal("0"),
        provider_cost_usdt=Decimal("0"),
    )
    paid = PartnerPrice(
        model_id=model.id,
        mode="input_tokens",
        resolution="default",
        billing_unit="million_tokens",
        price_rub=Decimal("1"),
        provider_cost_usdt=Decimal(".009"),
    )
    unreviewed = PartnerPrice(
        model_id=model.id,
        mode="cache_write_tokens",
        resolution="wrong-tier",
        billing_unit="million_tokens",
        price_rub=Decimal("0"),
        provider_cost_usdt=Decimal("0"),
    )
    db_session.add_all([free, paid, unreviewed])
    await db_session.flush()
    false_incident = FinancialIncident(
        kind=f"margin:{free.id}", episode="old-free-rate-alarm", recovered=False, muted=False, detail="Old alarm"
    )
    db_session.add(false_incident)
    await db_session.flush()

    await financial_tick(db_session)

    assert false_incident.recovered is True
    assert (await db_session.get(FinancialIncident, f"margin:{paid.id}")).recovered is False
    assert (await db_session.get(FinancialIncident, f"margin:{unreviewed.id}")).recovered is False
    assert notify.await_count == 2
