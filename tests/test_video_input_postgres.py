"""Real PostgreSQL admission locks, with only synthetic in-memory media HTTP."""

import asyncio
import copy
import os
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_native_inference import setup
from test_video_input_staging import BODY, install_transport

from app.accounts.models import ApiKey, Partner
from app.api.dependencies import PartnerAuth
from app.billing.models import CoverageLedgerEntry, LedgerEntry
from app.generations.models import Generation
from app.inference.service import reserve
from app.infrastructure.database import Base
from app.media import video_inputs


@pytest.fixture
async def media_postgres_sessions():
    url = os.getenv("TEST_POSTGRES_DATABASE_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_DATABASE_URL is not configured")
    schema = "media_admission_" + uuid4().hex
    admin = create_async_engine(url)
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest.mark.integration
@pytest.mark.parametrize("duplicate", [False, True])
async def test_media_preflight_does_not_lock_out_ordinary_or_duplicate_admission(
    media_postgres_sessions, monkeypatch, duplicate,
):
    sessions = media_postgres_sessions
    async with sessions() as db:
        partner, _, upstream = await setup(
            db, monkeypatch, lambda _: pytest.fail("No paid provider HTTP in admission"),
            model="seedance-2.5", category="video",
            rates=[("default", "720p", "second", Decimal("20"), Decimal(".078"))],
        )
        partner_id = partner.id
    # Keep the real credential selection / short session, while replacing only
    # the external free-upload adapter and media transport with synthetic data.
    real_upload_adapter = video_inputs._upload_adapter
    calls, uploads, tickets = install_transport(monkeypatch)
    synthetic_adapter = video_inputs._upload_adapter
    monkeypatch.setattr(video_inputs, "_upload_adapter", real_upload_adapter)
    monkeypatch.setattr("app.providers.service.get_partner_provider_adapter", synthetic_adapter)
    reached_upload, finish_upload = asyncio.Event(), asyncio.Event()
    real_stage = video_inputs._stage

    async def paused_stage(*args, **kwargs):
        reached_upload.set()
        await finish_upload.wait()
        return await real_stage(*args, **kwargs)

    monkeypatch.setattr(video_inputs, "_stage", paused_stage)

    async def admit(body, key):
        async with sessions() as db:
            # A leaked preflight row lock fails here instead of hanging CI.
            await db.execute(text("SET LOCAL lock_timeout = '1500ms'"))
            auth = PartnerAuth(
                partner=await db.get(Partner, partner_id),
                api_key=await db.scalar(select(ApiKey).where(ApiKey.partner_id == partner_id)),
            )
            generation, _ = await reserve(db, auth, "videos/generations", body, key)
            return generation.id

    measured = asyncio.create_task(admit(copy.deepcopy(BODY), "measured"))
    try:
        await asyncio.wait_for(reached_upload.wait(), timeout=5)
        ordinary_body = copy.deepcopy(BODY) if duplicate else {
            "model": "seedance-2.5", "prompt": "Ordinary", "duration": 4, "resolution": "720p",
        }
        # Must commit while the first preparation is still paused. This detects
        # both the old Credential→Partner inversion and a held Partner row lock.
        ordinary_id = await asyncio.wait_for(
            admit(ordinary_body, "measured" if duplicate else "ordinary"), timeout=5,
        )
        assert not measured.done()
        finish_upload.set()
        measured_id = await asyncio.wait_for(measured, timeout=5)
        async with sessions() as db:
            generations = list(await db.scalars(select(Generation)))
            retail = list(await db.scalars(select(LedgerEntry)))
            procurement = list(await db.scalars(select(CoverageLedgerEntry)))
            assert len(generations) == len(retail) == len(procurement) == (1 if duplicate else 2)
            assert (measured_id == ordinary_id) is duplicate
            assert all(item.operation_type == "generation_reserve" for item in retail)
            assert all(item.operation_type == "provider_cost_reserve" for item in procurement)
            partner = await db.get(Partner, partner_id)
            assert partner.balance_rub == Decimal("1000000") - sum(g.partner_price_rub for g in generations)
            if not duplicate:
                measured_row = await db.get(Generation, measured_id)
                assert measured_row.request_payload["reserved_units"] == {"seconds": 10}
        assert len(uploads) == len(tickets) == 1 and len(calls) == 3
    finally:
        finish_upload.set()
        if not measured.done():
            measured.cancel()
        await asyncio.gather(measured, return_exceptions=True)
        await upstream.aclose()
