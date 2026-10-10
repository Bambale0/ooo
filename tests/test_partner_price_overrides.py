from decimal import Decimal

from sqlalchemy import func, select

from app.accounts.models import Partner
from app.catalog.models import Model, PartnerPrice, PartnerPriceOverrideHistory, PartnerPriceSnapshot
from app.catalog.pricing import effective_partner_price, publish_global_partner_price, snapshot_partner_prices


async def _seed_two_partners(db_session):
    apix = Partner(telegram_id="test-apix-override", company_name="APIX", project_name="APIX", status="active")
    other = Partner(telegram_id="test-standard-rate", company_name="Other", project_name="Other", status="active")
    model = Model(slug="seedance-2.5", name="Seedance 2.5", modality="video", status="production")
    db_session.add_all([apix, other, model])
    await db_session.flush()
    template = PartnerPrice(
        model_id=model.id,
        mode="default",
        resolution="720p",
        price_rub=Decimal("23.80"),
        provider_cost_usdt=Decimal("0.10"),
        billing_unit="second",
    )
    db_session.add(template)
    await db_session.flush()
    await snapshot_partner_prices(db_session, apix.id)
    await snapshot_partner_prices(db_session, other.id)
    # Failed HTTP requests roll back the shared fixture session; persist test setup first.
    await db_session.commit()
    return apix, other, model, template


async def test_admin_can_lock_one_partner_rate_across_future_global_price_changes(
    client, db_session, admin_headers
):
    apix, other, model, template = await _seed_two_partners(db_session)
    payload = {
        "model_slug": model.slug,
        "mode": "default",
        "resolution": "720p",
        "price_rub": "23.00",
        "reason": "Negotiated partner-specific rate",
    }
    url = f"/api/v1/catalog/pricing/partners/{apix.id}"
    response = await client.put(url, json=payload, headers=admin_headers)
    assert response.status_code == 200, response.text
    assert Decimal(response.json()["price_rub"]) == Decimal("23.00")
    assert response.json()["is_custom"] is True

    repeated = await client.put(url, json=payload, headers=admin_headers)
    assert repeated.status_code == 200
    audit_count = (
        await db_session.execute(select(func.count()).select_from(PartnerPriceOverrideHistory))
    ).scalar_one()
    assert audit_count == 1

    # A temporary match with the global price must not erase the durable override.
    assert await publish_global_partner_price(db_session, template, Decimal("23.00")) == 1
    assert await publish_global_partner_price(db_session, template, Decimal("26.00")) == 1

    apix_rate = await effective_partner_price(
        db_session, partner_id=apix.id, model_id=model.id, mode="default", resolution="720p"
    )
    other_rate = await effective_partner_price(
        db_session, partner_id=other.id, model_id=model.id, mode="default", resolution="720p"
    )
    assert apix_rate is not None and apix_rate.price_rub == Decimal("23.00")
    assert other_rate is not None and other_rate.price_rub == Decimal("26.00")
    assert template.price_rub == Decimal("26.00")
    locked = await db_session.get(PartnerPriceSnapshot, (apix.id, template.id))
    assert locked is not None and locked.is_custom is True


async def test_partner_rate_override_requires_admin_and_rejects_below_cost(client, db_session, admin_headers):
    apix, other, model, template = await _seed_two_partners(db_session)
    url = f"/api/v1/catalog/pricing/partners/{apix.id}"
    payload = {
        "model_slug": model.slug,
        "mode": "default",
        "resolution": "720p",
        "price_rub": "0.01",
        "reason": "Below cost is forbidden",
    }
    anonymous = await client.put(url, json=payload)
    assert anonymous.status_code == 401
    too_low = await client.put(url, json=payload, headers=admin_headers)
    assert too_low.status_code == 409
    assert too_low.json()["detail"] == "partner_price_below_provider_cost"
    invalid_precision = await client.put(
        url, json={**payload, "price_rub": "23.001"}, headers=admin_headers
    )
    assert invalid_precision.status_code == 422
    snapshots = (
        await db_session.execute(
            select(PartnerPriceSnapshot).where(PartnerPriceSnapshot.partner_price_id == template.id)
        )
    ).scalars().all()
    assert len(snapshots) == 2
    assert {row.partner_id for row in snapshots} == {apix.id, other.id}
    assert all(row.price_rub == Decimal("23.80") and not row.is_custom for row in snapshots)


async def test_global_procurement_change_cannot_invalidate_locked_partner_rate(
    client, db_session, admin_headers
):
    apix, _other, model, template = await _seed_two_partners(db_session)
    url = f"/api/v1/catalog/pricing/partners/{apix.id}"
    rate_response = await client.put(
        url,
        json={
            "model_slug": model.slug,
            "mode": "default",
            "resolution": "720p",
            "price_rub": "23.00",
            "reason": "Negotiated partner-specific rate",
        },
        headers=admin_headers,
    )
    assert rate_response.status_code == 200
    response = await client.put(
        "/api/v1/catalog/pricing",
        headers=admin_headers,
        json={
            "model_slug": model.slug,
            "mode": "default",
            "resolution": "720p",
            "price_rub": "300.00",
            "provider_cost_usdt": "2.000000",
            "billing_unit": "second",
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "custom_partner_price_below_provider_cost"
    # AsyncSession rollback expires loaded ORM attributes; refresh explicitly.
    await db_session.refresh(template)
    assert template.price_rub == Decimal("23.80")
    assert template.provider_cost_usdt == Decimal("0.10")


async def test_global_unit_change_cannot_reinterpret_custom_per_second_rate(
    client, db_session, admin_headers
):
    apix, _other, model, template = await _seed_two_partners(db_session)
    response = await client.put(
        f"/api/v1/catalog/pricing/partners/{apix.id}",
        headers=admin_headers,
        json={
            "model_slug": model.slug,
            "mode": "default",
            "resolution": "720p",
            "price_rub": "23.00",
            "reason": "Negotiated per-second rate",
        },
    )
    assert response.status_code == 200
    global_response = await client.put(
        "/api/v1/catalog/pricing",
        headers=admin_headers,
        json={
            "model_slug": model.slug,
            "mode": "default",
            "resolution": "720p",
            "price_rub": "30.00",
            "provider_cost_usdt": "0.100000",
            "billing_unit": "generation",
        },
    )
    assert global_response.status_code == 409
    assert global_response.json()["detail"] == "custom_partner_billing_unit_conflict"
    await db_session.refresh(template)
    assert template.billing_unit == "second"
