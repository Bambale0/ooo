import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.accounts.models import Partner
from app.generations.models import Generation
from app.infrastructure.database import Base
from app.workers.generation_worker import (
    _load_candidate_ids,
    _run_bounded,
    process_generation_work_concurrently_once,
)


async def test_run_bounded_limits_concurrency():
    active = 0
    max_active = 0
    lock = asyncio.Lock()

    async def handler(item: int) -> bool:
        nonlocal active, max_active
        async with lock:
            active += 1
            max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        async with lock:
            active -= 1
        return True

    processed = await _run_bounded(
        list(range(8)),
        concurrency=3,
        handler=handler,
    )

    assert processed == 8
    assert max_active == 3


async def test_concurrent_worker_dispatches_candidates_in_parallel(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'worker.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    active = 0
    max_active = 0
    lock = asyncio.Lock()

    async def fake_dispatch(db, generation, provider):
        nonlocal active, max_active
        assert provider == "argolink"
        async with lock:
            active += 1
            max_active = max(max_active, active)
        await asyncio.sleep(0.03)
        async with lock:
            active -= 1
        return None

    async def fake_poll(db, generation, provider):
        raise AssertionError("poll should not run for queued-only fixture")

    monkeypatch.setattr(
        "app.workers.generation_worker.dispatch_generation_to_provider",
        fake_dispatch,
    )
    monkeypatch.setattr(
        "app.workers.generation_worker.poll_generation_provider",
        fake_poll,
    )

    async with session_factory() as db:
        partner = Partner(
            telegram_id="worker-concurrency",
            company_name="Concurrency",
            project_name="Concurrency Bot",
        )
        db.add(partner)
        await db.flush()
        for index in range(6):
            db.add(
                Generation(
                    partner_id=partner.id,
                    model_id=f"model-{index}",
                    model_slug="seedance-2.5",
                    mode="text_to_video",
                    resolution="720p",
                    duration_seconds=5,
                    idempotency_key=f"worker-concurrency-{index}",
                    partner_price_rub=Decimal("100.00"),
                    prompt="concurrency test",
                    status="queued",
                )
            )
        await db.commit()

    try:
        result = await process_generation_work_concurrently_once(
            session_factory=session_factory,
            limit=6,
            submit_concurrency=2,
            poll_concurrency=2,
        )
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()

    assert result.dispatched == 6
    assert result.polled == 0
    assert max_active == 2



async def test_candidate_selection_round_robins_partners(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fair-worker.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    base_time = datetime.now(UTC)
    async with session_factory() as db:
        partner_a = Partner(
            telegram_id="fair-a",
            company_name="Partner A",
            project_name="A",
        )
        partner_b = Partner(
            telegram_id="fair-b",
            company_name="Partner B",
            project_name="B",
        )
        db.add_all([partner_a, partner_b])
        await db.flush()

        generations: list[Generation] = []
        for index in range(4):
            generations.append(
                Generation(
                    partner_id=partner_a.id,
                    model_id=f"model-a-{index}",
                    model_slug="seedance-2.5",
                    mode="text_to_video",
                    resolution="720p",
                    duration_seconds=5,
                    idempotency_key=f"fair-a-{index}",
                    partner_price_rub=Decimal("100.00"),
                    prompt="fairness",
                    status="queued",
                    created_at=base_time + timedelta(seconds=index),
                )
            )
        for index in range(2):
            generations.append(
                Generation(
                    partner_id=partner_b.id,
                    model_id=f"model-b-{index}",
                    model_slug="seedance-2.5",
                    mode="text_to_video",
                    resolution="720p",
                    duration_seconds=5,
                    idempotency_key=f"fair-b-{index}",
                    partner_price_rub=Decimal("100.00"),
                    prompt="fairness",
                    status="queued",
                    created_at=base_time + timedelta(seconds=10 + index),
                )
            )
        db.add_all(generations)
        await db.commit()

        a_ids = [generation.id for generation in generations[:4]]
        b_ids = [generation.id for generation in generations[4:]]

    try:
        queued_ids, active_ids = await _load_candidate_ids(
            session_factory=session_factory,
            limit=4,
        )
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()

    assert queued_ids == [a_ids[0], b_ids[0], a_ids[1], b_ids[1]]
    assert active_ids == []
