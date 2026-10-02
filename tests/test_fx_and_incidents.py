from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.billing.fx import current_fx as real_current_fx
from app.billing.fx import refresh_fx_for_invoice, set_fx_policy
from app.billing.incidents import mute_treasury, observe_incident
from app.billing.models import FinancialIncident, FxRateSnapshot
from app.infrastructure.config import get_settings
from app.payments.crypto_pay import CryptoPayError
from app.telegram.models import BotNotification


async def test_fx_is_network_free_except_invoice_refresh_and_manual_mode_disables_auto(db_session, monkeypatch):
    client = AsyncMock()
    client.get_rub_per_usdt.return_value = Decimal("91.250000")
    stale = FxRateSnapshot(rate=Decimal("90.123456"), created_at=datetime.now(UTC) - timedelta(days=2))
    db_session.add(stale)
    await set_fx_policy(
        db_session,
        automatic_enabled=True,
        rate=Decimal("95"),
        actor="999",
        reason="Automatic with fallback",
    )
    await db_session.flush()

    current = await real_current_fx(db_session)
    assert current["source"] == "automatic"
    assert current["rate"] == Decimal("90.123456")
    client.get_rub_per_usdt.assert_not_awaited()

    refreshed = await refresh_fx_for_invoice(db_session, client=client)
    assert refreshed["source"] == "automatic"
    assert refreshed["rate"] == Decimal("91.250000")
    client.get_rub_per_usdt.assert_awaited_once()

    client.reset_mock()
    await set_fx_policy(
        db_session,
        automatic_enabled=False,
        rate=Decimal("97.500000"),
        actor="999",
        reason="Manual mode",
    )
    manual = await refresh_fx_for_invoice(db_session, client=client)
    assert manual["source"] == "manual"
    assert manual["rate"] == Decimal("97.500000")
    client.get_rub_per_usdt.assert_not_awaited()


async def test_incident_repeat_mute_recovery_and_new_episode(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_telegram_id", "999")
    kwargs = dict(kind="treasury", detail="Test deficit", repeating=True)
    await observe_incident(db_session, negative=True, **kwargs)
    incident = await db_session.get(FinancialIncident, "treasury")
    first_episode = incident.episode
    await observe_incident(db_session, negative=True, **kwargs)
    assert (await db_session.execute(select(func.count()).select_from(BotNotification))).scalar() == 1
    incident.last_alert_at = datetime.now(UTC) - timedelta(minutes=16)
    # Advance clock into the next dedupe interval too.
    monkeypatch.setattr("app.billing.incidents.utc_now", lambda: datetime.now(UTC) + timedelta(minutes=16))
    await observe_incident(db_session, negative=False, **kwargs)
    assert (await db_session.execute(select(func.count()).select_from(BotNotification))).scalar() == 2
    await mute_treasury(db_session, first_episode)
    await observe_incident(db_session, negative=False, **kwargs)
    assert incident.muted
    await observe_incident(db_session, negative=True, **kwargs)
    assert incident.episode != first_episode and not incident.muted
    assert (await db_session.execute(select(func.count()).select_from(BotNotification))).scalar() == 3
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        await mute_treasury(db_session, first_episode)
