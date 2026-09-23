from decimal import Decimal

from sqlalchemy import func, select

from app.billing.incidents import financial_tick
from app.billing.margins import overrides, set_threshold, threshold
from app.billing.models import MarginThresholdHistory
from app.catalog.models import Model, PartnerPrice
from app.infrastructure.config import get_settings
from app.telegram.models import BotNotification


async def test_hierarchy_history_and_alert_episodes_without_price_changes(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_telegram_id", "999")
    model = Model(slug="grok-imagine-video-1.5", name="Grok", modality="video", status="production")
    db_session.add(model)
    await db_session.flush()
    price = PartnerPrice(
        model_id=model.id,
        mode="default",
        resolution="720p",
        billing_unit="second",
        price_rub=Decimal(100),
        provider_cost_usdt=Decimal(".75"),
    )
    db_session.add(price)
    await db_session.flush()
    await financial_tick(db_session)
    await financial_tick(db_session)
    assert (await db_session.execute(select(func.count()).select_from(BotNotification))).scalar() == 1
    await set_threshold(db_session, actor="999", value=Decimal(20))
    await financial_tick(db_session)  # lowering below current margin only resolves future evaluation
    assert (await db_session.execute(select(func.count()).select_from(BotNotification))).scalar() == 1
    await set_threshold(db_session, actor="999", value=Decimal(40), model=model.slug)
    await set_threshold(db_session, actor="999", value=Decimal(10), model=model.slug, mode="default", resolution="720p")
    assert threshold(await overrides(db_session), model.id, price.id) == 10
    await financial_tick(db_session)
    await set_threshold(db_session, actor="999", value=None, model=model.slug, mode="default", resolution="720p")
    assert threshold(await overrides(db_session), model.id, price.id) == 40
    await financial_tick(db_session)
    assert (await db_session.execute(select(func.count()).select_from(BotNotification))).scalar() == 2
    assert price.price_rub == 100 and price.provider_cost_usdt == Decimal(".75")
    rows = list(
        (await db_session.execute(select(MarginThresholdHistory).order_by(MarginThresholdHistory.id))).scalars()
    )
    assert len(rows) == 4 and rows[0].old_value == 30
    assert rows[-1].old_value == 10 and rows[-1].new_value is None


async def test_threshold_admin_auth_validation_and_history(client, admin_headers):
    route = "/api/v1/billing/margin-thresholds"
    assert (await client.post(route, json={"value": "25"})).status_code == 401
    assert (await client.post(route, json={"value": "101"}, headers=admin_headers)).status_code == 422
    assert (await client.post(route, json={"value": "20", "mode": "default"}, headers=admin_headers)).status_code == 422
    result = await client.post(route, json={"value": "25"}, headers=admin_headers)
    assert result.status_code == 200
    assert result.json()["old_value"] == 30
    history = (await client.get(route, headers=admin_headers)).json()
    assert len(history) == 1 and history[0]["actor"] == "admin_api"
