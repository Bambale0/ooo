import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.infrastructure.config import get_settings
from app.providers.circuit import admit, locked, observe, open_circuit, recovery_tick
from app.providers.models import ProviderCircuit, ProviderOutcome
from app.telegram.models import BotNotification


def generation(status="completed"):
    return SimpleNamespace(id=str(uuid4()), status=status, public_error_code=None)


async def test_threshold_recovery_serial_probes_and_accelerated_reopen(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_telegram_id", "999")
    clock = [datetime.now(UTC)]
    monkeypatch.setattr("app.providers.circuit.utc_now", lambda: clock[0])
    for i in range(20):
        await observe(db_session, generation("failed" if i >= 17 else "completed"))
    row = await db_session.get(ProviderCircuit, "argolink")
    assert row.state == "open"
    assert not await admit(db_session, "denied")
    check = AsyncMock(return_value=True)
    await recovery_tick(db_session, health_check=check)
    check.assert_not_called()
    for i in range(3):
        clock[0] += timedelta(seconds=60)
        await recovery_tick(db_session, health_check=check)
        assert row.state == ("recovering" if i == 2 else "open")
    assert check.await_count == 3
    for i in range(3):
        probe = generation()
        assert await admit(db_session, probe.id)
        assert not await admit(db_session, "another-probe")
        await db_session.commit()  # recovery claim survives restart/session expiration
        await observe(db_session, probe)
        await observe(db_session, probe)  # callback replay is not a second successful probe
        assert row.real_successes == i + 1
    assert row.state == "closed"
    assert await admit(db_session, "ordinary")
    await observe(db_session, generation("failed"))
    assert row.state == "open" and row.episode == 2
    assert (await db_session.execute(select(func.count()).select_from(BotNotification))).scalar() == 3


async def test_small_sample_and_neutral_rejections_do_not_disable_all_partners(db_session):
    for _ in range(19):
        await observe(db_session, generation("failed"))
    assert (await db_session.get(ProviderCircuit, "argolink")).state == "closed"
    for _ in range(30):
        await observe(db_session, generation("failed"), outcome="neutral")
    assert (await db_session.get(ProviderCircuit, "argolink")).state == "closed"


async def test_failed_free_check_resets_streak_and_lost_probe_reopens(db_session, monkeypatch):
    row = await locked(db_session, "argolink")
    await open_circuit(db_session, row)
    clock = [datetime.now(UTC) + timedelta(minutes=1)]
    monkeypatch.setattr("app.providers.circuit.utc_now", lambda: clock[0])
    check = AsyncMock(side_effect=[True, False, True, True, True])
    for _ in range(5):
        await recovery_tick(db_session, health_check=check)
        clock[0] += timedelta(minutes=1)
    assert row.state == "recovering" and row.healthy_checks == 3
    assert await admit(db_session, "lost-probe")
    clock[0] += timedelta(seconds=get_settings().worker_provider_processing_timeout_seconds + 1)
    await recovery_tick(db_session, health_check=check)
    assert row.state == "open" and not row.in_flight
    assert check.await_count == 5  # never launches a generation as a health check


@pytest.mark.integration
async def test_postgres_recovery_allows_exactly_one_concurrent_request():
    url = os.getenv("TEST_POSTGRES_DATABASE_URL")
    if not url:
        pytest.skip("PostgreSQL required")
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    provider = f"test-{uuid4()}"
    try:
        async with sessions() as db:
            row = await locked(db, provider)
            row.state = "recovering"
            await db.commit()

        async def claim():
            async with sessions() as db:
                allowed = await admit(db, str(uuid4()), provider=provider)
                await db.commit()
                return allowed

        assert sum(await asyncio.gather(*(claim() for _ in range(5)))) == 1
    finally:
        async with sessions() as db:
            await db.execute(delete(ProviderOutcome).where(ProviderOutcome.provider == provider))
            await db.execute(delete(ProviderCircuit).where(ProviderCircuit.provider == provider))
            await db.commit()
        await engine.dispose()
