from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.billing.fx import current_fx as real_current_fx
from app.billing.fx import set_manual_fallback
from app.billing.incidents import mute_treasury, observe_incident
from app.billing.models import FinancialIncident, FxRateSnapshot
from app.infrastructure.config import get_settings
from app.payments.crypto_pay import CryptoPayError
from app.telegram.models import BotNotification


async def test_fx_automatic_manual_last_automatic_order(db_session, monkeypatch):
    client = AsyncMock()
    client.get_rub_per_usdt.return_value = Decimal("90.123456")
    monkeypatch.setattr("app.billing.fx.get_crypto_pay_client", lambda: client)
    automatic = await real_current_fx(db_session)
    assert automatic["source"] == "automatic" and automatic["rate"] == Decimal("90.123456")
    assert automatic["automatic_at"]
    stored = (await db_session.execute(select(FxRateSnapshot))).scalar_one()
    stored.created_at = datetime.now(UTC) - timedelta(minutes=2)
    await db_session.flush()
    client.get_rub_per_usdt.side_effect = CryptoPayError("offline")
    await set_manual_fallback(db_session, rate=Decimal("95"), actor="999", reason="Fallback")
    manual = await real_current_fx(db_session)
    assert manual["source"] == "manual_fallback" and manual["rate"] == Decimal("95")
    await set_manual_fallback(db_session, rate=None, actor="999", reason="Disabled")
    # SQLite now() has second precision; explicitly order the latest setting in this test.
    from app.billing.models import FxFallbackSetting

    settings = list((await db_session.execute(select(FxFallbackSetting))).scalars())
    settings[-1].created_at = datetime.now(UTC) + timedelta(seconds=1)
    await db_session.flush()
    last = await real_current_fx(db_session)
    assert last["source"] == "last_automatic" and last["rate"] == automatic["rate"]
    client.get_rub_per_usdt.side_effect = None
    client.get_rub_per_usdt.return_value = Decimal("91")
    recovered = await real_current_fx(db_session)
    assert recovered["source"] == "automatic" and recovered["rate"] == Decimal("91")


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
