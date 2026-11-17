"""Real storage races, run by CI after applying the production migration chain."""

import asyncio
import os
import secrets
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import Request
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.accounts.models import Partner, PartnerIdentity
from app.dashboard import web, yandex
from app.infrastructure.config import get_settings


@pytest.mark.integration
async def test_real_redis_consumes_flow_once_and_expires(monkeypatch):
    if not os.getenv("TEST_POSTGRES_DATABASE_URL"):
        pytest.skip("CI infrastructure is not configured")
    state, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    await yandex._save_flow(state, browser, {"mode": "login", "verifier": "secret"})
    assert await yandex._take_flow(state, "wrong-browser") is None
    results = await asyncio.gather(*(yandex._take_flow(state, browser) for _ in range(8)))
    assert sum(result is not None for result in results) == 1
    assert await yandex._take_flow(state, browser) is None
    # Verify TTL on the actual stored key, then force expiry without sleeps.
    import redis.asyncio as redis

    client = redis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        await yandex._save_flow(state, browser, {"mode": "login"})
        key = yandex._flow_key(state, browser)
        assert 0 < await client.ttl(key) <= 300
        await client.expire(key, 0)
        assert await yandex._take_flow(state, browser) is None
    finally:
        await client.aclose()


@pytest.mark.integration
@pytest.mark.parametrize("same_partner", [False, True])
async def test_concurrent_callbacks_cannot_share_or_replace_identity(monkeypatch, same_partner):
    url = os.getenv("TEST_POSTGRES_DATABASE_URL")
    if not url:
        pytest.skip("CI PostgreSQL is not configured")
    s = get_settings()
    monkeypatch.setattr(s, "public_api_base_url", "https://testserver")
    monkeypatch.setattr(s, "yandex_client_id", "client")
    monkeypatch.setattr(s, "yandex_client_secret", SecretStr("test-client-secret"))
    monkeypatch.setattr(s, "yandex_callback_url", "https://testserver/cabinet/yandex/callback")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid4().hex
    partners = [
        Partner(
            telegram_id=f"oauth-{suffix}-{index}",
            company_name="OAuth race",
            project_name="OAuth race",
            balance_rub=Decimal("25.00"),
        )
        for index in range(2)
    ]
    ids = []
    try:
        async with factory() as db:
            db.add_all(partners)
            await db.commit()
            ids = [partner.id for partner in partners]
        states = [secrets.token_urlsafe(32) for _ in range(2)]
        browser = secrets.token_urlsafe(32)
        sessions = {f"session-{i}": ids[0] if same_partner else ids[i] for i in range(2)}
        flows = {
            states[i]: {
                "mode": "link",
                "partner_id": sessions[f"session-{i}"],
                "session_key": f"session-{i}",
                "verifier": "verifier",
            }
            for i in range(2)
        }
        barrier = asyncio.Event()
        waiting = 0

        async def take(state, nonce):
            assert nonce == browser
            return flows.pop(state)

        async def bound(key, *, destroy=False):
            return sessions.pop(key, None) if destroy else sessions.get(key)

        async def subject(code, verifier):
            nonlocal waiting
            waiting += 1
            if waiting == 2:
                barrier.set()
            await asyncio.wait_for(barrier.wait(), 10)
            return code

        issued = []

        async def store(token, partner_id):
            issued.append(partner_id)

        monkeypatch.setattr(yandex, "_take_flow", take)
        monkeypatch.setattr(yandex, "_bound_session", bound)
        monkeypatch.setattr(yandex, "_subject", subject)
        monkeypatch.setattr(web, "_store_session", store)
        subjects = [str(int(uuid4().hex[:20], 16)) for _ in range(2)]
        if not same_partner:
            subjects[1] = subjects[0]

        async def finish(index):
            request = Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/cabinet/yandex/callback",
                    "headers": [(b"cookie", f"{yandex._COOKIE}={browser}".encode())],
                    "query_string": f"state={states[index]}&code={subjects[index]}".encode(),
                }
            )
            async with factory() as db:
                response = await yandex.callback(request, db)
                return response.status_code

        results = await asyncio.wait_for(asyncio.gather(finish(0), finish(1)), 20)
        assert sorted(results) == [200, 409]
        assert len(issued) == 1
        async with factory() as db:
            identities = await db.execute(select(PartnerIdentity).where(PartnerIdentity.partner_id.in_(ids)))
            assert len(identities.scalars().all()) == 1
            balances = await db.scalars(select(Partner.balance_rub).where(Partner.id.in_(ids)))
            assert list(balances) == [Decimal("25.00"), Decimal("25.00")]
    finally:
        if ids:
            async with factory() as db:
                await db.execute(delete(PartnerIdentity).where(PartnerIdentity.partner_id.in_(ids)))
                await db.execute(delete(Partner).where(Partner.id.in_(ids)))
                await db.commit()
        await engine.dispose()
