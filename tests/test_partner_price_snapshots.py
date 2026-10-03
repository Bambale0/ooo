from decimal import Decimal

from sqlalchemy import select

from app.accounts.models import Partner
from app.billing.incidents import financial_tick
from app.billing.models import FinancialIncident
from app.catalog.models import Model, PartnerPrice, PartnerPriceSnapshot
from app.catalog.pricing import effective_partner_price, publish_global_partner_price, snapshot_partner_prices
from app.infrastructure.config import get_settings


async def test_existing_partner_retail_stays_frozen_while_procurement_moves(db_session):
    partner = Partner(telegram_id="snapshot-old", company_name="Old", project_name="Old", status="active")
    model = Model(slug="seedance-2.5", name="Seedance 2.5", modality="video", status="production")
    db_session.add_all([partner, model])
    await db_session.flush()
    price = PartnerPrice(
        model_id=model.id,
        mode="text_to_video",
        resolution="720p",
        price_rub=Decimal("100.00"),
        provider_cost_usdt=Decimal("0.50"),
        billing_unit="second",
    )
    db_session.add(price)
    await db_session.flush()
    await snapshot_partner_prices(db_session, partner.id)

    effective = await effective_partner_price(
        db_session,
        partner_id=partner.id,
        model_id=model.id,
        mode="text_to_video",
        resolution="720p",
    )
    assert effective is not None
    assert effective.price_rub == Decimal("100.00")
    assert effective.provider_cost_usdt == Decimal("0.50")

    price.price_rub = Decimal("140.00")
    price.provider_cost_usdt = Decimal("0.90")
    await db_session.flush()

    effective = await effective_partner_price(
        db_session,
        partner_id=partner.id,
        model_id=model.id,
        mode="text_to_video",
        resolution="720p",
    )
    assert effective is not None
    assert effective.price_rub == Decimal("100.00")
    assert effective.provider_cost_usdt == Decimal("0.90")


async def test_new_partner_snapshots_current_template_price(db_session):
    model = Model(slug="seedance-2.0", name="Seedance 2.0", modality="video", status="production")
    db_session.add(model)
    await db_session.flush()
    price = PartnerPrice(
        model_id=model.id,
        mode="text_to_video",
        resolution="720p",
        price_rub=Decimal("120.00"),
        provider_cost_usdt=Decimal("0.40"),
        billing_unit="second",
    )
    db_session.add(price)
    partner = Partner(telegram_id="snapshot-new", company_name="New", project_name="New", status="active")
    db_session.add(partner)
    await db_session.flush()

    assert await snapshot_partner_prices(db_session, partner.id) == 1
    snapshot = (
        await db_session.execute(
            select(PartnerPriceSnapshot).where(
                PartnerPriceSnapshot.partner_id == partner.id,
                PartnerPriceSnapshot.partner_price_id == price.id,
            )
        )
    ).scalar_one()
    assert snapshot.price_rub == Decimal("120.00")


async def test_confirmed_global_price_publication_updates_existing_partner_for_new_generations(db_session):
    partner = Partner(telegram_id="snapshot-publish", company_name="Publish", project_name="Publish", status="active")
    model = Model(slug="seedance-2.0", name="Seedance 2.0", modality="video", status="production")
    db_session.add_all([partner, model])
    await db_session.flush()
    price = PartnerPrice(
        model_id=model.id,
        mode="default",
        resolution="720p",
        price_rub=Decimal("20.00"),
        provider_cost_usdt=Decimal("0.10"),
        billing_unit="second",
    )
    db_session.add(price)
    await db_session.flush()
    await snapshot_partner_prices(db_session, partner.id)

    updated = await publish_global_partner_price(db_session, price, Decimal("22.00"))
    effective = await effective_partner_price(
        db_session,
        partner_id=partner.id,
        model_id=model.id,
        mode="default",
        resolution="720p",
    )

    assert updated == 1
    assert price.price_rub == Decimal("22.00")
    assert effective is not None and effective.price_rub == Decimal("22.00")


async def test_partner_negative_margin_creates_incident(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_telegram_id", "999")
    partner = Partner(telegram_id="snapshot-margin", company_name="Margin", project_name="Margin", status="active")
    model = Model(slug="seedance-2.5", name="Seedance 2.5", modality="video", status="production")
    db_session.add_all([partner, model])
    await db_session.flush()
    price = PartnerPrice(
        model_id=model.id,
        mode="text_to_video",
        resolution="720p",
        price_rub=Decimal("100.00"),
        provider_cost_usdt=Decimal("2.00"),
        billing_unit="second",
    )
    db_session.add(price)
    await db_session.flush()
    db_session.add(
        PartnerPriceSnapshot(
            partner_id=partner.id,
            partner_price_id=price.id,
            price_rub=Decimal("100.00"),
        )
    )
    await db_session.flush()

    await financial_tick(db_session)
    incident = await db_session.get(FinancialIncident, f"partner-economics:{partner.id}:{price.id}")
    assert incident is not None
    assert incident.recovered is False
    assert "Negative margin" in incident.detail
