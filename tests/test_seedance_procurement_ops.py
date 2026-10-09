import importlib.util
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, inspect, select
from test_infai_fallback import seed

from app.catalog.models import Model, PartnerPrice, PartnerPriceHistory


async def test_operator_apply_is_atomic_idempotent_and_keeps_accepted_prices(db_session, monkeypatch, capsys):
    from contextlib import asynccontextmanager

    from app.providers.argolink import ArgoLinkAdapter
    from app.providers.models import ProviderCredit
    from ops import seedance25_procurement as ops

    _, generation, _, _ = await seed(db_session)
    model = Model(id=generation.model_id, slug="seedance-2.5", name="Seedance", modality="video")
    db_session.add(model)
    for resolution, retail, cost in [("480p", "12", ".078"), ("720p", "23.80", ".170"), ("1080p", "57.13", ".430")]:
        db_session.add(
            PartnerPrice(
                model_id=model.id,
                mode="default",
                resolution=resolution,
                price_rub=Decimal(retail),
                provider_cost_usdt=Decimal(cost),
                billing_unit="second",
            )
        )
    await db_session.commit()

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr(ops, "SessionLocal", session)
    await ops.run()
    assert list(await db_session.scalars(select(ProviderCredit))) == []
    assert list(await db_session.scalars(select(PartnerPriceHistory))) == []

    def handler(request):
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "seedance-2.5",
                        "pricing": {
                            "effective": {
                                "currency": "USD",
                                "unit": "second",
                                "tiers": [
                                    {"label": "480p", "price": "0.0874"},
                                    {"label": "720p", "price": "0.196"},
                                    {"label": "1080p", "price": "0.483"},
                                ],
                            }
                        },
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://argolink.io") as client:
        monkeypatch.setattr(ops, "ArgoLinkAdapter", lambda: ArgoLinkAdapter(client=client, api_key="test"))
        await ops.run(apply=True)
        await ops.run(apply=True)
    assert len(list(await db_session.scalars(select(ProviderCredit)))) == 1
    history = list(await db_session.scalars(select(PartnerPriceHistory)))
    assert len(history) == 3
    assert all(row.old_price_rub == row.new_price_rub for row in history)
    await db_session.refresh(generation)
    assert generation.partner_price_rub == Decimal("327")
    assert generation.request_payload["rates"]["seconds"]["retail"] == "21.8"
    assert "supplier_credit_record_id" in capsys.readouterr().out


async def test_operator_rejects_changed_live_prices_before_writing(monkeypatch):
    from app.providers.argolink import ArgoLinkAdapter
    from ops import seedance25_procurement as ops

    def handler(request):
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "seedance-2.5",
                        "pricing": {
                            "effective": {
                                "currency": "USD",
                                "unit": "second",
                                "tiers": [{"label": "480p", "price": ".99"}],
                            }
                        },
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://argolink.io") as client:
        monkeypatch.setattr(ops, "ArgoLinkAdapter", lambda: ArgoLinkAdapter(client=client, api_key="test"))
        with pytest.raises(ValueError, match="supplier_procurement_changed_again"):
            await ops.run(apply=True)


def test_provider_credit_migration_upgrade_downgrade():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path(__file__).parents[1] / "alembic/versions/20261008_0028_provider_credits.py"
    spec = importlib.util.spec_from_file_location("provider_credit_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            assert "provider_credits" in inspect(connection).get_table_names()
            assert len(inspect(connection).get_columns("provider_credits")) == 8
            module.downgrade()
            assert "provider_credits" not in inspect(connection).get_table_names()
    engine.dispose()
