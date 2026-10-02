from decimal import Decimal

from sqlalchemy import select

from app.accounts.models import Partner
from app.catalog.models import Model, PartnerPrice
from app.inference.service import quote


def _image_price(*, model_id: str, partner_id: str | None, tier: str, retail: str, cost: str) -> PartnerPrice:
    return PartnerPrice(
        model_id=model_id,
        partner_id=partner_id,
        mode="default",
        resolution=tier,
        price_rub=Decimal(retail),
        provider_cost_usdt=Decimal(cost),
        billing_unit="generation",
    )


def test_legacy_retail_uses_current_global_procurement_cost():
    prices = []
    for tier in ("1K", "2K", "4K"):
        prices.append(
            _image_price(
                model_id="model",
                partner_id=None,
                tier=tier,
                retail="2.50",
                cost="0.015",
            )
        )
        prices.append(
            _image_price(
                model_id="model",
                partner_id="legacy-partner",
                tier=tier,
                retail="1.93",
                cost="0.001",
            )
        )

    rates, units, resolution = quote(
        "images/generations",
        {"model": "nano-banana-2", "resolution": "1K"},
        prices,
        fx=Decimal("100"),
    )

    assert resolution == "1K"
    assert units == {"1K": 1}
    assert Decimal(rates["1K"]["retail"]) == Decimal("1.93")
    assert Decimal(rates["1K"]["cost"]) == Decimal("0.015")


async def test_public_pricing_never_exposes_partner_override(client, db_session):
    partner = Partner(
        telegram_id="pricing-private",
        company_name="Private",
        project_name="Private",
    )
    model = Model(
        slug="pricing-private-model",
        name="Pricing Private",
        modality="image",
        status="production",
    )
    db_session.add_all([partner, model])
    await db_session.flush()
    db_session.add_all(
        [
            PartnerPrice(
                model_id=model.id,
                partner_id=None,
                mode="default",
                resolution="1K",
                price_rub=Decimal("10.00"),
                provider_cost_usdt=Decimal("0.05"),
                billing_unit="generation",
            ),
            PartnerPrice(
                model_id=model.id,
                partner_id=partner.id,
                mode="default",
                resolution="1K",
                price_rub=Decimal("5.00"),
                provider_cost_usdt=Decimal("0.05"),
                billing_unit="generation",
            ),
        ]
    )
    await db_session.commit()

    response = await client.get(f"/api/v1/catalog/pricing?partner_id={partner.id}")

    assert response.status_code == 200
    rows = response.json()
    row = next(item for item in rows if item["model_slug"] == model.slug)
    assert Decimal(row["price_rub"]) == Decimal("10.00")
    assert "partner_id" not in row


async def test_snapshot_records_current_public_price_without_changing_global(db_session):
    from app.catalog.pricing import snapshot_global_prices_for_partner

    partner = Partner(
        telegram_id="legacy-snapshot",
        company_name="Legacy",
        project_name="Legacy",
    )
    model = Model(
        slug="legacy-snapshot-model",
        name="Legacy Snapshot",
        modality="image",
        status="production",
    )
    db_session.add_all([partner, model])
    await db_session.flush()
    global_price = PartnerPrice(
        model_id=model.id,
        partner_id=None,
        mode="default",
        resolution="1K",
        price_rub=Decimal("3.85"),
        provider_cost_usdt=Decimal("0.03"),
        billing_unit="generation",
    )
    db_session.add(global_price)
    await db_session.commit()

    created = await snapshot_global_prices_for_partner(db_session, partner.id)

    assert created == 1
    override = (
        await db_session.execute(
            select(PartnerPrice).where(
                PartnerPrice.partner_id == partner.id,
                PartnerPrice.model_id == model.id,
            )
        )
    ).scalar_one()
    assert override.price_rub == Decimal("3.85")
    await db_session.refresh(global_price)
    assert global_price.price_rub == Decimal("3.85")


async def test_capital_reserve_uses_legacy_retail_with_current_global_cost(db_session):
    from app.billing.capital import capital_state

    partner = Partner(
        telegram_id="legacy-capital",
        company_name="Legacy Capital",
        project_name="Legacy Capital",
        balance_rub=Decimal("100.00"),
    )
    model = Model(
        slug="legacy-capital-model",
        name="Legacy Capital",
        modality="image",
        status="production",
    )
    db_session.add_all([partner, model])
    await db_session.flush()
    db_session.add_all(
        [
            PartnerPrice(
                model_id=model.id,
                partner_id=None,
                mode="default",
                resolution="1K",
                price_rub=Decimal("3.00"),
                provider_cost_usdt=Decimal("0.02"),
                billing_unit="generation",
            ),
            PartnerPrice(
                model_id=model.id,
                partner_id=partner.id,
                mode="default",
                resolution="1K",
                price_rub=Decimal("1.50"),
                provider_cost_usdt=Decimal("0.01"),
                billing_unit="generation",
            ),
        ]
    )
    await db_session.commit()

    state = await capital_state(db_session)

    assert state["components"]["current_future_cost_reserve_usdt"] == Decimal("1.333333333333333333333333333")
