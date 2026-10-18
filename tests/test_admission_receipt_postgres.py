"""Real PostgreSQL proves mutually exclusive rejected and accepted identities."""

import asyncio
import json
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
import test_video_input_postgres as pg_fixtures
from fastapi import Request
from sqlalchemy import select, text
from test_native_inference import setup

from app.accounts.models import ApiKey, Partner
from app.api.dependencies import PartnerAuth
from app.billing.models import CoverageLedgerEntry, LedgerEntry
from app.generations.models import Generation
from app.inference import router as routes
from app.inference.admission_receipts import fence_rejected_admission
from app.providers.argolink import ArgoLinkAdapter
from app.providers.models import ProviderAttempt

receipt_postgres_sessions = pg_fixtures.media_postgres_sessions
BODY = {"model": "seedance-2.5", "prompt": "test", "duration": 4, "resolution": "480p"}
RID = "d3252064-e54b-4590-a56f-ae162ef7b9ec"


def request(headers):
    async def receive():
        return {"type": "http.request", "body": json.dumps(BODY).encode(), "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/videos/generations",
            "headers": [
                (k.lower().encode(), v.encode())
                for k, v in {
                    **headers,
                    "Content-Type": "application/json",
                    "X-Client-Request-Id": RID,
                    "X-Submission-Receipt": "v1",
                }.items()
            ],
        },
        receive=receive,
    )


async def seeded(sessions, monkeypatch):
    async with sessions() as db:
        partner, headers, upstream = await setup(
            db,
            monkeypatch,
            lambda _: pytest.fail("No provider POST in admission tests"),
            model="seedance-2.5",
            category="video",
            rates=[("default", "480p", "second", Decimal("20"), Decimal(".078"))],
        )
        return partner.id, headers, upstream


async def invoke(sessions, partner_id, headers):
    async with sessions() as db:
        await db.execute(text("SET LOCAL lock_timeout = '2000ms'"))
        auth = PartnerAuth(
            partner=await db.get(Partner, partner_id),
            api_key=await db.scalar(select(ApiKey).where(ApiKey.partner_id == partner_id)),
        )
        return await routes.inference("videos/generations", request(headers), db, auth)


@pytest.mark.integration
async def test_concurrent_rejection_fences_cannot_become_paid_after_funds_recover(
    receipt_postgres_sessions, monkeypatch
):
    sessions = receipt_postgres_sessions
    partner_id, headers, upstream = await seeded(sessions, monkeypatch)
    async with sessions() as db:
        key_id = await db.scalar(select(ApiKey.id).where(ApiKey.partner_id == partner_id))

    async def fence():
        async with sessions() as db:
            g = await fence_rejected_admission(
                db,
                partner_id=partner_id,
                api_key_id=key_id,
                protocol="videos/generations",
                body=BODY,
                idempotency_key=headers["Idempotency-Key"],
                client_request_id=RID,
            )
            return g.id

    try:
        ids = await asyncio.wait_for(asyncio.gather(*(fence() for _ in range(10))), timeout=15)
        assert len(set(ids)) == 1
        monkeypatch.setattr(ArgoLinkAdapter, "prepaid_balance_usdt", AsyncMock(return_value=Decimal("1000000")))
        result = await invoke(sessions, partner_id, headers)
        assert result.status_code == 503 and result.headers["X-Submission-State"] == "not-accepted-v1"
        async with sessions() as db:
            g = (await db.execute(select(Generation))).scalar_one()
            assert g.status == "failed" and g.billing_status == "not_chargeable"
            for model in (ProviderAttempt, LedgerEntry, CoverageLedgerEntry):
                assert (await db.execute(select(model))).scalars().all() == []
            assert (await db.get(Partner, partner_id)).balance_rub == Decimal("1000000")
    finally:
        await upstream.aclose()


@pytest.mark.integration
async def test_overlapping_acceptance_after_rollback_prevents_rejection_receipt(receipt_postgres_sessions, monkeypatch):
    sessions = receipt_postgres_sessions
    partner_id, headers, upstream = await seeded(sessions, monkeypatch)
    rolled_back, allow_fence = asyncio.Event(), asyncio.Event()
    original_fence = routes.fence_rejected_admission

    async def paused_fence(*args, **kwargs):
        rolled_back.set()
        await allow_fence.wait()
        return await original_fence(*args, **kwargs)

    monkeypatch.setattr(routes, "fence_rejected_admission", paused_fence)
    balance = AsyncMock(return_value=Decimal(0))
    monkeypatch.setattr(ArgoLinkAdapter, "prepaid_balance_usdt", balance)
    first = asyncio.create_task(invoke(sessions, partner_id, headers))
    try:
        await asyncio.wait_for(rolled_back.wait(), timeout=5)
        balance.return_value = Decimal("1000000")
        winner = await asyncio.wait_for(invoke(sessions, partner_id, headers), timeout=5)
        assert winner.status_code == 202
        allow_fence.set()
        recovered = await asyncio.wait_for(first, timeout=5)
        assert recovered.status_code == 202 and recovered.body == winner.body
        assert "X-Submission-State" not in recovered.headers
        async with sessions() as db:
            g = (await db.execute(select(Generation))).scalar_one()
            assert g.status == "queued" and "native_admission_rejection" not in g.request_payload
    finally:
        allow_fence.set()
        if not first.done():
            first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await upstream.aclose()


@pytest.mark.integration
async def test_rotating_concurrent_keys_cannot_exceed_partner_storage_quota(receipt_postgres_sessions, monkeypatch):
    from types import SimpleNamespace
    from uuid import uuid4

    from fastapi import HTTPException

    from app.inference import admission_receipts

    sessions = receipt_postgres_sessions
    partner_id, headers, upstream = await seeded(sessions, monkeypatch)
    monkeypatch.setattr(
        admission_receipts, "get_settings", lambda: SimpleNamespace(native_rejection_receipts_max_per_partner=2)
    )
    async with sessions() as db:
        key_id = await db.scalar(select(ApiKey.id).where(ApiKey.partner_id == partner_id))
        other = Partner(telegram_id=str(uuid4()), company_name="Other", project_name="Other")
        db.add(other)
        await db.commit()
        other_id = other.id

    async def fence(index, tenant=partner_id):
        async with sessions() as db:
            try:
                await fence_rejected_admission(
                    db,
                    partner_id=tenant,
                    api_key_id=key_id,
                    protocol="videos/generations",
                    body=BODY,
                    idempotency_key=f"quota-key-{index}",
                    client_request_id=f"quota-request-{index}",
                )
                return True
            except HTTPException as error:
                assert error.status_code == 503
                return False

    try:
        results = await asyncio.wait_for(asyncio.gather(*(fence(i) for i in range(10))), timeout=15)
        assert sum(results) == 2
        assert await fence(99, tenant=other_id)
        async with sessions() as db:
            rows = (await db.execute(select(Generation))).scalars().all()
            assert len(rows) == 3
            assert sum(g.partner_id == partner_id for g in rows) == 2
            for model in (ProviderAttempt, LedgerEntry, CoverageLedgerEntry):
                assert (await db.execute(select(model))).scalars().all() == []
    finally:
        await upstream.aclose()
