"""Run a bounded synthetic worker scenario against the loopback provider stub."""

from __future__ import annotations

import argparse
import asyncio
import os
import time

import httpx
from sqlalchemy import func, select

from app.accounts.models import Partner
from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.infrastructure.database import SessionLocal
from app.providers.http_client import close_provider_http_clients
from app.providers.models import ProviderAttempt
from app.workers.generation_worker import process_generation_work_concurrently_once
from ops.load.worker_seed import PREFIX, _assert_safe_worker_environment, clean, seed

TERMINAL = {"completed", "failed", "cancelled", "reconciliation_required"}


async def _counts() -> dict[str, int]:
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(Generation.status, func.count(Generation.id))
                .join(Partner, Partner.id == Generation.partner_id)
                .where(Partner.telegram_id.like(f"{PREFIX}%"))
                .group_by(Generation.status)
            )
        ).all()
    return {status: int(count) for status, count in rows}


async def _assert_invariants(expected_total: int) -> None:
    async with SessionLocal() as db:
        generations = list(
            (
                await db.execute(
                    select(Generation)
                    .join(Partner, Partner.id == Generation.partner_id)
                    .where(Partner.telegram_id.like(f"{PREFIX}%"))
                )
            ).scalars().all()
        )
        generation_ids = [generation.id for generation in generations]
        attempts = list(
            (
                await db.execute(
                    select(ProviderAttempt).where(ProviderAttempt.generation_id.in_(generation_ids))
                )
            ).scalars().all()
        )

    if len(generations) != expected_total:
        raise RuntimeError(f"Expected {expected_total} generations, found {len(generations)}")
    if any(generation.status != "completed" for generation in generations):
        bad = {}
        for generation in generations:
            if generation.status != "completed":
                bad[generation.status] = bad.get(generation.status, 0) + 1
        raise RuntimeError(f"Non-completed generations remain: {bad}")
    if len(attempts) != expected_total:
        raise RuntimeError(f"Expected one provider attempt per generation, found {len(attempts)}")
    task_ids = [attempt.provider_task_id for attempt in attempts]
    if any(task_id is None for task_id in task_ids):
        raise RuntimeError("A completed synthetic generation is missing provider_task_id")
    if len(set(task_ids)) != expected_total:
        raise RuntimeError("Duplicate provider_task_id detected in synthetic run")


async def _stub_stats(base_url: str) -> dict[str, object]:
    async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
        response = await client.get("/__load__/stats")
        response.raise_for_status()
        return response.json()


async def run(*, timeout_seconds: float) -> None:
    _assert_safe_worker_environment()
    settings = get_settings()
    partners = int(os.getenv("LOAD_WORKER_PARTNERS", "5"))
    per_partner = int(os.getenv("LOAD_WORKER_GENERATIONS_PER_PARTNER", "100"))
    expected_total = partners * per_partner
    stub_base_url = settings.argolink_base_url.rstrip("/")

    await clean()
    await seed()
    started = time.monotonic()
    try:
        while True:
            counts = await _counts()
            active = sum(count for state, count in counts.items() if state not in TERMINAL)
            if active == 0:
                break
            if time.monotonic() - started > timeout_seconds:
                raise RuntimeError(f"Worker resilience scenario timed out with counts={counts}")

            await process_generation_work_concurrently_once(
                limit=settings.worker_batch_size,
                submit_concurrency=settings.worker_submit_concurrency,
                poll_concurrency=settings.worker_poll_concurrency,
            )
            await asyncio.sleep(0.05)

        await _assert_invariants(expected_total)
        stats = await _stub_stats(stub_base_url)
        print(f"Worker resilience scenario PASS: generations={expected_total}, counts={await _counts()}, stub={stats}")
    finally:
        await close_provider_http_clients()
        await clean()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    args = parser.parse_args()
    await run(timeout_seconds=args.timeout_seconds)


if __name__ == "__main__":
    asyncio.run(main())
