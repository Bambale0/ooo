"""Regression: repeated Argolink image body stalls must protect new primary submissions."""
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
from sqlalchemy import select
from test_native_api_diagnostics import nano_body, nano_setup

from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.providers.circuit import (
    admit,
    native_image_circuit_provider,
    observe,
    recovery_tick,
)
from app.providers.models import ProviderAttempt, ProviderCircuit


async def test_native_image_circuit_is_model_scoped_and_opens_on_short_failure_burst(db_session, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "argolink_native_image_circuit_min_samples", 6)
    monkeypatch.setattr(settings, "argolink_native_image_circuit_failure_ratio", Decimal("0.5"))
    scope = native_image_circuit_provider("nano-banana-pro")
    assert scope == "argolink:native:nano-banana-pro"
    assert native_image_circuit_provider("seedance-2.5") is None
    for i in range(6):
        gen = SimpleNamespace(
            id=str(uuid4()), status="completed" if i < 2 else "failed",
            public_error_code=None if i < 2 else "provider_response_invalid",
        )
        await observe(db_session, gen, provider=scope)
    assert (await db_session.get(ProviderCircuit, scope)).state == "open"
    assert not await admit(db_session, str(uuid4()), provider=scope)
    assert await admit(db_session, str(uuid4()), provider="argolink:native:nano-banana-2.1")
    assert await admit(db_session, str(uuid4()), provider="argolink")


async def test_native_image_circuit_rejects_before_paid_upstream_post(client, db_session, monkeypatch):
    scope = native_image_circuit_provider("nano-banana-pro")
    row = ProviderCircuit(provider=scope, state="open", episode=1, healthy_checks=0, real_successes=0)
    db_session.add(row)
    await db_session.commit()
    requests = []
    def handler(request):
        requests.append(request)
        raise AssertionError("Provider must not receive a rejected circuit-open submission")
    partner, headers, upstream = await nano_setup(db_session, monkeypatch, handler)
    response = await client.post("/v1/images/edits", headers=headers, json=nano_body())
    assert response.status_code == 503
    assert response.json()["detail"] == "provider_circuit_open"
    assert requests == []
    assert not (await db_session.scalars(select(Generation))).all()
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("1000000")
    await upstream.aclose()


async def test_native_image_timeout_is_classified_and_never_replayed(client, db_session, monkeypatch):
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(0.10)
            yield b'{"data":[]}'

    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=SlowBody())

    partner, headers, upstream = await nano_setup(db_session, monkeypatch, handler)
    monkeypatch.setattr(get_settings(), "native_request_timeout_seconds", 0.02)
    response = await client.post("/v1/images/edits", headers=headers, json=nano_body())
    assert response.status_code == 503
    generation = (await db_session.scalars(select(Generation))).one()
    attempt = (await db_session.scalars(select(ProviderAttempt))).one()
    assert generation.public_error_code == "request_deadline_exceeded"
    assert attempt.status == "reconciliation_required"
    assert generation.billing_status == "not_chargeable"
    duplicate = await client.post("/v1/images/edits", headers=headers, json=nano_body())
    assert duplicate.json() == response.json()
    assert len(calls) == 1
    await upstream.aclose()


async def test_native_image_circuit_recovers_with_free_argolink_health_checks(db_session, monkeypatch):
    scope = native_image_circuit_provider("nano-banana-pro")
    row = ProviderCircuit(provider=scope, state="open", episode=1, healthy_checks=0, real_successes=0)
    db_session.add(row)
    await db_session.commit()
    clock = [datetime.now(UTC)]
    monkeypatch.setattr("app.providers.circuit.utc_now", lambda: clock[0])
    health = AsyncMock(return_value=True)
    queried = []
    def get_adapter(provider):
        queried.append(provider)
        if provider != "argolink":
            raise AssertionError("Scoped circuits must check the physical provider, not a virtual name")
        return SimpleNamespace(health_check=health)
    monkeypatch.setattr("app.providers.registry.get_provider_adapter", get_adapter)
    for index in range(3):
        clock[0] += timedelta(minutes=1)
        await recovery_tick(db_session, scope)
        assert row.state == ("recovering" if index == 2 else "open")
    assert queried == ["argolink"] * 3
    assert health.await_count == 3
    # Recovery grants ONE real request. It must never synthesize a paid probe.
    assert await admit(db_session, "probe-1", provider=scope)
    assert not await admit(db_session, "probe-2", provider=scope)
