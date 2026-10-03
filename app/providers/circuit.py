"""Durable provider admission gate. Polling accepted jobs always continues.

Twenty terminal outcomes is the initial minimum traffic sample. Recovery runs
three free checks one minute apart, then three real requests strictly one at a
time. No paid synthetic probes, retries or altered request parameters.
"""

from datetime import UTC, timedelta

from fastapi import HTTPException
from sqlalchemy import select, text

from app.infrastructure.config import get_settings
from app.infrastructure.retry import utc_now
from app.providers.models import ProviderCircuit, ProviderOutcome
from app.telegram.service import notify


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


async def locked(db, provider):
    if db.bind.dialect.name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": "circuit:" + provider}
        )
    row = await db.get(ProviderCircuit, provider, with_for_update=True, populate_existing=True)
    if row is None:
        row = ProviderCircuit(provider=provider, state="closed", episode=0, healthy_checks=0, real_successes=0)
        db.add(row)
        await db.flush()
    return row


async def open_circuit(db, row):
    if row.state == "open":
        return
    row.state, row.in_flight, row.probe_started_at = "open", None, None
    row.healthy_checks = row.real_successes = 0
    row.episode += 1
    row.last_check_at = utc_now()
    await notify(
        db,
        get_settings().admin_telegram_id,
        f"Поставщик {row.provider} временно отключён из-за ошибок. "
        "Принятые задачи продолжают проверяться; новые ожидают восстановления.",
        f"circuit:{row.provider}:{row.episode}:open",
    )


async def admit(db, generation_id, *, provider="argolink", claim=True):
    row = await locked(db, provider)
    if row.state == "open" or (row.state == "recovering" and row.in_flight not in {None, generation_id}):
        return False
    if row.state == "recovering" and claim:
        row.in_flight, row.probe_started_at = generation_id, utc_now()
    return True


async def require_admission(db, generation_id, *, claim=True):
    if not await admit(db, generation_id, claim=claim):
        raise HTTPException(503, "provider_temporarily_unavailable", headers={"Retry-After": "60"})


async def observe(db, generation, *, outcome=None, provider="argolink"):
    if outcome is None:
        if generation.status == "completed":
            outcome = "success"
        elif generation.status == "timeout":
            outcome = "timeout"
        elif generation.status in {"failed", "reconciliation_required"}:
            outcome = (
                "neutral"
                if generation.public_error_code
                in {
                    "provider_rejected_request",
                    "provider_rate_limited",
                    "content_policy_violation",
                    "invalid_generation_request",
                    "provider_authentication_failed",
                }
                else "error"
            )
        elif generation.status == "cancelled":
            outcome = "neutral"
        else:
            return
    row = await locked(db, provider)
    if await db.get(ProviderOutcome, generation.id):
        return
    now = utc_now()
    db.add(ProviderOutcome(generation_id=generation.id, provider=provider, outcome=outcome, created_at=now))
    await db.flush()
    failed = outcome in {"error", "timeout"}
    if row.state == "recovering":
        if failed:
            await open_circuit(db, row)
        elif row.in_flight == generation.id:
            row.in_flight, row.probe_started_at = None, None
            row.real_successes = row.real_successes + 1 if outcome == "success" else 0
            if row.real_successes >= 3:
                row.state, row.recovered_at = "closed", now
                await notify(
                    db,
                    get_settings().admin_telegram_id,
                    f"Поставщик {provider} восстановлен: 3 успешных реальных запроса.",
                    f"circuit:{provider}:{row.episode}:recovered",
                )
    elif row.state == "closed" and failed:
        if row.recovered_at and aware(row.recovered_at) > now - timedelta(minutes=10):
            await open_circuit(db, row)
        else:
            cutoff = (
                max(now - timedelta(minutes=10), aware(row.recovered_at))
                if row.recovered_at
                else now - timedelta(minutes=10)
            )
            samples = list(
                (
                    await db.execute(
                        select(ProviderOutcome).where(
                            ProviderOutcome.provider == provider,
                            ProviderOutcome.created_at >= cutoff,
                            ProviderOutcome.outcome != "neutral",
                        )
                    )
                ).scalars()
            )
            recent = [s for s in samples if aware(s.created_at) >= now - timedelta(minutes=5)]
            errors = sum(s.outcome in {"error", "timeout"} for s in recent)
            timeouts = sum(s.outcome == "timeout" for s in samples)
            if (len(recent) >= 20 and errors * 10 > len(recent)) or (
                len(samples) >= 20 and timeouts * 20 > len(samples)
            ):
                await open_circuit(db, row)
    await db.flush()


async def recovery_tick(db, provider="argolink", *, health_check=None):
    row = await locked(db, provider)
    now = utc_now()
    if row.state == "recovering":
        if row.probe_started_at and aware(row.probe_started_at) < now - timedelta(
            seconds=get_settings().worker_provider_processing_timeout_seconds
        ):
            # A crashed/ambiguous probe is never submitted again by recovery.
            await open_circuit(db, row)
        return
    if row.state != "open" or (row.last_check_at and aware(row.last_check_at) > now - timedelta(seconds=60)):
        return
    if health_check is None:
        from app.providers.registry import get_provider_adapter

        if provider == "infai":
            from app.providers.service import get_active_provider_credential, get_partner_provider_adapter

            if await get_active_provider_credential(db, "", provider) is None:
                row.last_check_at = now
                return
            health_check = (await get_partner_provider_adapter(db, "", provider)).health_check
        else:
            health_check = get_provider_adapter(provider).health_check
    row.last_check_at = now
    try:
        healthy = await health_check()
    except Exception:
        healthy = False
    row.healthy_checks = row.healthy_checks + 1 if healthy else 0
    if row.healthy_checks >= 3:
        row.state, row.real_successes, row.in_flight = "recovering", 0, None
    await db.flush()
