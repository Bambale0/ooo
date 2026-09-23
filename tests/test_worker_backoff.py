from datetime import timedelta
from decimal import Decimal

from app.accounts.models import Partner
from app.generations.models import Generation
from app.infrastructure.retry import utc_now
from app.providers.models import ProviderAttempt
from app.workers.generation_worker import _fair_candidate_ids_query


async def test_deferred_retry_does_not_starve_fresh_queued_work(db_session):
    partner = Partner(telegram_id="backoff", company_name="Test", project_name="Test")
    db_session.add(partner)
    await db_session.flush()
    jobs = []
    for index in range(2):
        job = Generation(
            partner_id=partner.id,
            model_id="test",
            model_slug="seedance-2.5",
            mode="text_to_video",
            resolution="720p",
            duration_seconds=5,
            idempotency_key=f"backoff-{index}",
            partner_price_rub=Decimal("20"),
            prompt="test",
            status="queued",
        )
        db_session.add(job)
        await db_session.flush()
        jobs.append(job)
    db_session.add(
        ProviderAttempt(
            generation_id=jobs[0].id,
            provider="argolink",
            status="retry_pending",
            next_attempt_at=utc_now() + timedelta(minutes=5),
        )
    )
    await db_session.flush()
    candidates = (await db_session.execute(_fair_candidate_ids_query(("queued",), limit=1))).scalars().all()
    assert candidates == [jobs[1].id]
