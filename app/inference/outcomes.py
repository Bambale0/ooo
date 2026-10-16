"""Durable customer finality, separate from the provider's cost obligation."""

from decimal import Decimal

from sqlalchemy import select

from app.billing.service import release_generation_cost_reserve, release_generation_reserve
from app.generations.models import Generation
from app.infrastructure.retry import utc_now
from app.webhooks.service import ensure_terminal_webhook_event


async def finalize_native_failure(db, generation, attempt, code, *, definitive):
    """Caller commits customer outcome, billing disposition and outbox together.

    A missing provider result never turns into a later customer debit. Unknown
    procurement remains an explicit attempt obligation for cost-only review.
    """
    await db.execute(
        select(Generation)
        .where(Generation.id == generation.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if generation.actual_charge_rub is not None or generation.billing_status == "charged":
        return False
    if generation.billing_status in {"not_chargeable", "waived"}:
        await ensure_terminal_webhook_event(db, generation)
        return False
    generation.status = "failed"
    generation.billing_status = "not_chargeable"
    generation.client_finalized_at = utc_now()
    generation.public_error_code = code
    attempt.public_error_code = code
    attempt.next_poll_at = attempt.next_attempt_at = None
    attempt.status = "failed" if definitive else "reconciliation_required"
    await release_generation_reserve(db, generation, reason="Customer request closed without a result")
    if definitive:
        attempt.cost_status = "confirmed_free"
        attempt.provider_cost_usdt = Decimal(0)
        attempt.cost_reserve_usdt = Decimal(0)
        generation.provider_cost_hold_usdt = Decimal(0)
        generation.provider_cost_hold_rub = Decimal(0)
        await release_generation_cost_reserve(db, generation, reason="Provider definitively rejected request")
    else:
        obligation = Decimal(
            generation.provider_cost_reserve_usdt
            if generation.provider_cost_reserve_usdt is not None
            else generation.provider_cost_usdt_snapshot
        )
        attempt.cost_status = "unknown"
        attempt.cost_reserve_usdt = obligation
        generation.provider_cost_hold_usdt = obligation
        generation.provider_cost_hold_rub = generation.provider_cost_reserve_rub
    await ensure_terminal_webhook_event(db, generation)
    await db.flush()
    return True


async def preserve_unbilled_result(db, generation, attempt, response):
    """Deliver a validated result without inventing usage or a customer price."""
    from app.inference.replay import save_response

    await db.execute(
        select(Generation)
        .where(Generation.id == generation.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if generation.billing_status in {"not_chargeable", "waived"}:
        return False
    generation.status = attempt.status = "completed"
    generation.client_finalized_at = generation.client_finalized_at or utc_now()
    generation.public_error_code = None
    generation.request_payload = {**(generation.request_payload or {}), "usage_reconciliation_pending": True}
    attempt.cost_status = "unknown"
    attempt.cost_reserve_usdt = (
        generation.provider_cost_reserve_usdt
        if generation.provider_cost_reserve_usdt is not None
        else generation.provider_cost_usdt_snapshot
    )
    generation.provider_cost_hold_usdt = attempt.cost_reserve_usdt
    generation.provider_cost_hold_rub = generation.provider_cost_reserve_rub
    await save_response(db, generation, response)
    await ensure_terminal_webhook_event(db, generation)
    await db.commit()
    return True
