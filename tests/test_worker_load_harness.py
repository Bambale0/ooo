from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.accounts.models import Partner
from app.billing.models import CoverageLedgerEntry, LedgerEntry
from app.generations.models import Generation
from ops.load import provider_stub, worker_seed
from ops.load.worker_seed import ACK, _assert_safe_worker_environment


@pytest.fixture(autouse=True)
async def reset_stub(monkeypatch):
    monkeypatch.setenv("LOAD_STUB_KEY_PREFIX", "local-load-partner-")
    monkeypatch.setenv("LOAD_STUB_PROCESSING_POLLS", "1")
    monkeypatch.setenv("LOAD_STUB_SUBMIT_429_EVERY", "0")
    monkeypatch.setenv("LOAD_STUB_POLL_429_EVERY", "0")
    monkeypatch.setenv("LOAD_STUB_SUBMIT_500_EVERY", "0")
    monkeypatch.setenv("LOAD_STUB_POLL_500_EVERY", "0")
    transport = httpx.ASGITransport(app=provider_stub.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://stub") as client:
        response = await client.post("/__load__/reset", headers={"X-Load-Test-Ack": "I_UNDERSTAND"})
        assert response.status_code == 200
    yield


async def test_provider_stub_requires_partner_key_and_completes_after_configured_polls():
    transport = httpx.ASGITransport(app=provider_stub.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://stub") as client:
        unauthorized = await client.post(
            "/v1/videos/generations",
            json={"model": "seedance-2.5", "prompt": "test", "duration": 5},
        )
        assert unauthorized.status_code == 401

        headers = {"Authorization": "Bearer local-load-partner-1"}
        submit = await client.post(
            "/v1/videos/generations",
            headers=headers,
            json={"model": "seedance-2.5", "prompt": "test", "duration": 5},
        )
        assert submit.status_code == 202
        request_id = submit.json()["request_id"]

        first_poll = await client.get(f"/v1/videos/{request_id}", headers=headers)
        second_poll = await client.get(f"/v1/videos/{request_id}", headers=headers)
        assert first_poll.json()["status"] == "pending"
        assert second_poll.json()["status"] == "done"
        assert second_poll.json()["usage"]["billed_seconds"] == 5

        content = await client.get(f"/v1/videos/{request_id}/content", headers=headers)
        assert content.status_code == 200
        assert content.headers["content-type"].startswith("video/mp4")


async def test_provider_stub_can_inject_retry_after(monkeypatch):
    monkeypatch.setenv("LOAD_STUB_SUBMIT_429_EVERY", "1")
    monkeypatch.setenv("LOAD_STUB_RETRY_AFTER_SECONDS", "2.5")
    transport = httpx.ASGITransport(app=provider_stub.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://stub") as client:
        response = await client.post(
            "/v1/videos/generations",
            headers={"Authorization": "Bearer local-load-partner-1"},
            json={"model": "seedance-2.5", "prompt": "test", "duration": 5},
        )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "2.5"


def test_worker_fixture_requires_loopback_provider_and_explicit_ack(monkeypatch):
    settings = __import__("app.infrastructure.config", fromlist=["get_settings"]).get_settings()
    original_env = settings.app_env
    original_url = settings.argolink_base_url
    try:
        settings.app_env = "test"
        settings.argolink_base_url = "https://argolink.io"
        monkeypatch.setenv("LOAD_WORKER_TEST_ACK", ACK)
        with pytest.raises(RuntimeError, match="loopback"):
            _assert_safe_worker_environment()

        settings.argolink_base_url = "http://127.0.0.1:18080"
        monkeypatch.delenv("LOAD_WORKER_TEST_ACK", raising=False)
        with pytest.raises(RuntimeError, match="LOAD_WORKER_TEST_ACK"):
            _assert_safe_worker_environment()
    finally:
        settings.app_env = original_env
        settings.argolink_base_url = original_url


async def test_worker_clean_removes_generation_ledgers_before_partner(db_session, monkeypatch):
    partner = Partner(
        telegram_id="worker-load-clean-ledgers",
        company_name="Worker load cleanup",
        project_name="Worker load cleanup",
    )
    db_session.add(partner)
    await db_session.flush()
    generation = Generation(
        partner_id=partner.id,
        model_id="worker-load-model",
        model_slug="seedance-2.5",
        mode="text_to_video",
        resolution="720p",
        duration_seconds=5,
        idempotency_key="worker-load-clean-ledgers",
        partner_price_rub=Decimal("1.00"),
        provider_cost_usdt_snapshot=Decimal("0.01"),
        provider_cost_reserve_usdt=Decimal("0.01"),
        rub_per_usdt_snapshot=Decimal("100"),
        provider_cost_reserve_rub=Decimal("1.00"),
        prompt="cleanup",
        status="completed",
    )
    db_session.add(generation)
    await db_session.flush()
    db_session.add_all(
        [
            CoverageLedgerEntry(
                partner_id=partner.id,
                operation_type="provider_cost_reserve_adjustment",
                amount_rub=Decimal("0.01"),
                coverage_after_rub=Decimal("0.01"),
                idempotency_key="worker-load-clean-coverage",
                generation_id=generation.id,
            ),
            LedgerEntry(
                partner_id=partner.id,
                operation_type="generation_usage_adjustment",
                amount_rub=Decimal("0.01"),
                balance_after_rub=Decimal("0.01"),
                idempotency_key="worker-load-clean-retail",
                generation_id=generation.id,
            ),
        ]
    )
    await db_session.commit()

    class SessionContext:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(worker_seed, "SessionLocal", lambda: SessionContext())
    monkeypatch.setattr(worker_seed, "_assert_safe_worker_environment", lambda: None)

    await worker_seed.clean()

    assert (await db_session.execute(select(CoverageLedgerEntry))).scalars().all() == []
    assert (await db_session.execute(select(LedgerEntry))).scalars().all() == []
    assert (await db_session.execute(select(Generation))).scalars().all() == []
    assert (await db_session.execute(select(Partner))).scalars().all() == []
