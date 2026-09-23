import asyncio
import os
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_native_inference import setup

from app.accounts.models import ApiKey
from app.api.dependencies import PartnerAuth
from app.inference.accounting import settle_actual
from app.inference.service import reserve
from app.telegram.models import TrialEntitlement
from app.telegram.trials import claim_trial, download_link


async def test_trial_preserves_native_parameters_free_settlement_and_lifetime_limit(db_session, monkeypatch):
    partner, _, upstream = await setup(
        db_session,
        monkeypatch,
        lambda r: None,
        model="grok-imagine-video-1.5",
        category="video",
        rates=[("default", "720p", "second", Decimal(10), Decimal(".05"))],
    )
    partner.telegram_id, partner.balance_rub = "123", Decimal(0)
    auth = PartnerAuth(partner, ApiKey())
    body = {
        "model": "grok-imagine-video-1.5",
        "prompt": "reference",
        "duration": 6,
        "resolution": "720p",
        "image_url": "https://example.org/reference.jpg",
        "aspect_ratio": "16:9",
    }
    first, _ = await reserve(db_session, auth, "videos/generations", body, "trial-first", trial_telegram_id="123")
    replay, _ = await reserve(db_session, auth, "videos/generations", body, "trial-first", trial_telegram_id="123")
    assert replay.id == first.id
    assert first.request_payload["native_body"] == body and first.partner_price_rub == 0
    await settle_actual(db_session, first, {"seconds": 6})
    assert first.actual_charge_rub == 0 and first.actual_provider_cost_usdt == Decimal(".3")
    assert partner.balance_rub == 0
    await reserve(db_session, auth, "videos/generations", body, "trial-second", trial_telegram_id="123")
    assert (await db_session.get(TrialEntitlement, "123")).used == 2
    # Entitlement is keyed by identity, not partner id: re-registration cannot regrant it.
    with pytest.raises(HTTPException, match="trial_limit_reached"):
        await claim_trial(db_session, "123")
    assert partner.balance_rub == 0
    await upstream.aclose()


async def test_trial_flag_in_api_body_does_not_grant_free_billing(client, db_session, monkeypatch):
    partner, headers, upstream = await setup(
        db_session,
        monkeypatch,
        lambda r: None,
        model="grok-imagine-video-1.5",
        category="video",
        rates=[("default", "720p", "second", Decimal(10), Decimal(".05"))],
    )
    body = {"model": "grok-imagine-video-1.5", "prompt": "test", "duration": 6, "trial_telegram_id": "native-1"}
    result = await client.post("/v1/videos/generations", json=body, headers=headers)
    assert result.status_code == 202
    assert partner.balance_rub == Decimal("999940")
    assert not list((await db_session.execute(select(TrialEntitlement))).scalars())
    await upstream.aclose()


async def test_trial_download_tampering_expiry_and_paid_generation_denied(client, db_session, monkeypatch):
    partner, _, upstream = await setup(
        db_session,
        monkeypatch,
        lambda r: None,
        model="grok-imagine-video-1.5",
        category="video",
        rates=[("default", "720p", "second", Decimal(10), Decimal(".05"))],
    )
    first, _ = await reserve(
        db_session,
        PartnerAuth(partner, ApiKey()),
        "videos/generations",
        {"model": "grok-imagine-video-1.5", "prompt": "test", "duration": 6},
        "trial-link",
        trial_telegram_id=partner.telegram_id,
    )
    url = download_link(first).replace("http://localhost:8000", "")
    assert (await client.get(url[:-1] + ("a" if url[-1] != "a" else "b"))).status_code == 404
    parts = url.split("/")
    parts[-2] = "1"
    assert (await client.get("/".join(parts))).status_code == 404
    first.request_payload = {k: v for k, v in first.request_payload.items() if k != "trial_telegram_id"}
    await db_session.commit()
    assert (await client.get(url)).status_code == 404
    await upstream.aclose()


@pytest.mark.integration
async def test_postgres_concurrent_lifetime_trial_limit():
    url = os.getenv("TEST_POSTGRES_DATABASE_URL")
    if not url:
        pytest.skip("PostgreSQL required")
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    user = f"trial-test-{uuid4()}"
    try:

        async def claim():
            async with sessions() as db:
                try:
                    await claim_trial(db, user)
                    await db.commit()
                    return True
                except HTTPException as exc:
                    await db.rollback()
                    assert exc.detail == "trial_limit_reached"
                    return False

        assert sum(await asyncio.gather(*(claim() for _ in range(6)))) == 2
    finally:
        async with sessions() as db:
            await db.execute(delete(TrialEntitlement).where(TrialEntitlement.telegram_id == user))
            await db.commit()
        await engine.dispose()
