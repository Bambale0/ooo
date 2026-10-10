from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from test_native_inference import setup

from app.billing.fx import current_fx as read_internal_fx
from app.billing.fx import refresh_fx_for_invoice, restore_snapshot, set_fx_policy, snapshot
from app.billing.models import CoverageLedgerEntry, FxRateSnapshot, LedgerEntry
from app.generations.models import Generation
from app.inference.accounting import settle_actual
from app.infrastructure.config import Settings, get_settings
from app.payments.crypto_pay import CryptoPayError

FLOOR = Decimal("84.50")
OBSERVED = Decimal("83.296218")


@pytest.fixture
def internal_floor(monkeypatch):
    # setitem also reproduces the missing floor on the pre-fix settings model.
    monkeypatch.setitem(get_settings().__dict__, "internal_min_rub_per_usdt", FLOOR)


@pytest.mark.parametrize(
    "observed,expected",
    [(OBSERVED, FLOOR), (FLOOR, FLOOR), (Decimal("92.123456"), Decimal("92.123456"))],
)
async def test_internal_floor_preserves_higher_rates_and_source_rows(db_session, internal_floor, observed, expected):
    row = FxRateSnapshot(rate=observed)
    db_session.add(row)
    await db_session.commit()
    actual = await read_internal_fx(db_session)
    assert actual["rate"] == expected
    assert Decimal(actual["observed_rate"]) == observed
    assert Decimal(actual["minimum_rate"]) == FLOOR
    assert actual["source"] == "automatic"
    await db_session.refresh(row)
    assert row.rate == observed
    saved = snapshot(actual)
    assert restore_snapshot(saved) == actual


async def test_disabled_floor_retains_legacy_policy_shape(db_session, monkeypatch):
    monkeypatch.setitem(get_settings().__dict__, "internal_min_rub_per_usdt", None)
    db_session.add(FxRateSnapshot(rate=OBSERVED))
    await db_session.commit()
    actual = await read_internal_fx(db_session)
    assert actual["rate"] == OBSERVED
    assert "observed_rate" not in actual and "minimum_rate" not in actual


async def test_invoice_rate_is_not_replaced_by_internal_minimum(db_session, internal_floor):
    class Client:
        calls = 0

        async def get_rub_per_usdt(self):
            self.calls += 1
            return OBSERVED

    provider = Client()
    invoice_fx = await refresh_fx_for_invoice(db_session, client=provider)
    assert invoice_fx["rate"] == OBSERVED
    assert "minimum_rate" not in invoice_fx
    assert (await read_internal_fx(db_session))["rate"] == FLOOR
    assert (await read_internal_fx(db_session))["rate"] == FLOOR
    assert provider.calls == 1
    # A previously accepted invoice keeps its actual invoice exchange rate.
    assert restore_snapshot(snapshot(invoice_fx))["rate"] == OBSERVED


@pytest.mark.parametrize("manual", [True, False])
async def test_invoice_fallback_is_raw_but_internal_pricing_has_floor(db_session, internal_floor, manual):
    await set_fx_policy(
        db_session, automatic_enabled=not manual, rate=Decimal("80"), actor="test", reason="test policy"
    )

    class Unavailable:
        async def get_rub_per_usdt(self):
            if manual:
                raise AssertionError("Manual invoice mode must not call the provider")
            raise CryptoPayError("temporary outage")

    assert (await refresh_fx_for_invoice(db_session, client=Unavailable()))["rate"] == Decimal("80")
    assert (await read_internal_fx(db_session))["rate"] == FLOOR


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity"])
def test_invalid_floor_config_is_rejected(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, internal_min_rub_per_usdt=value)


@pytest.mark.parametrize("edit", [False, True])
async def test_seedance_cost_and_internal_fx_snapshots_settle_once(
    client, db_session, monkeypatch, internal_floor, edit
):
    def forbidden(request):
        raise AssertionError("No provider call needed for reserve/settlement verification")

    monkeypatch.setattr("app.billing.fx.current_fx", read_internal_fx)
    monkeypatch.setattr(get_settings(), "seedance_25_edit_markup_rub_per_second", Decimal("2.50"))
    db_session.add(FxRateSnapshot(rate=OBSERVED))
    partner, headers, upstream = await setup(
        db_session, monkeypatch, forbidden, model="seedance-2.5", category="video",
        rates=[
            ("default", "720p", "second", Decimal("23.80"), Decimal(".196")),
            ("edit", "720p", "second", Decimal("18.83"), Decimal(".196")),
        ],
    )
    body = {"model": "seedance-2.5", "prompt": "Warm colors", "resolution": "720p"}
    if edit:
        body.update(omni_reference_task_type="edit", reference_videos=[{"url": "https://example.org/clip.mp4"}])
    else:
        body["duration"] = 10
    try:
        response = await client.post("/v1/videos/generations", headers=headers, json=body)
        assert response.status_code == 202, response.text
        generation = await db_session.get(Generation, response.json()["request_id"])
        rate = Decimal(".196") * FLOOR + Decimal("2.50") if edit else Decimal("23.80")
        reserved_units = 60 if edit else 10
        procurement_rate = Decimal(".117") if edit else Decimal(".196")
        assert generation.rub_per_usdt_snapshot == FLOOR
        assert generation.provider_cost_usdt_snapshot == procurement_rate * reserved_units
        accepted = generation.request_payload
        assert Decimal(accepted["rates"]["seconds"]["retail"]) == rate
        assert Decimal(accepted["fx"]["rate"]) == FLOOR
        assert Decimal(accepted["fx"]["observed_rate"]) == OBSERVED
        monkeypatch.setitem(get_settings().__dict__, "internal_min_rub_per_usdt", Decimal("110"))
        duplicate = await client.post("/v1/videos/generations", headers=headers, json=body)
        assert duplicate.json()["request_id"] == generation.id
        billed_seconds = 20 if edit else 10
        await settle_actual(db_session, generation, {"seconds": billed_seconds})
        await settle_actual(db_session, generation, {"seconds": billed_seconds})
        await db_session.commit()
        await db_session.refresh(partner)
        expected_charge = (rate * billed_seconds).quantize(Decimal(".01"), rounding="ROUND_HALF_UP")
        assert generation.actual_charge_rub == expected_charge
        assert generation.actual_provider_cost_usdt == procurement_rate * billed_seconds
        assert generation.rub_per_usdt_snapshot == FLOOR and generation.request_payload == accepted
        assert partner.balance_rub == Decimal("1000000") - expected_charge
        coverage = list(await db_session.scalars(
            select(CoverageLedgerEntry).where(CoverageLedgerEntry.generation_id == generation.id)
        ))
        expected_coverage = (procurement_rate * billed_seconds * FLOOR).quantize(
            Decimal(".01"), rounding="ROUND_HALF_UP"
        )
        assert sum(x.amount_rub for x in coverage) == -expected_coverage
        ledger = list(await db_session.scalars(select(LedgerEntry).where(LedgerEntry.generation_id == generation.id)))
        assert len(ledger) == 2 and sum(x.amount_rub for x in ledger) == -expected_charge
    finally:
        await upstream.aclose()
