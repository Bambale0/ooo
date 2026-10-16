from datetime import UTC, timedelta
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.models import CoverageLedgerEntry
from app.billing.service import (
    apply_cost_coverage_change,
    lock_partner_for_update,
    settle_confirmed_partner_charge,
    settle_generation_reserves,
)
from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.infrastructure.retry import utc_now

TOKEN_MODES = ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens")
MILLION = Decimal(1_000_000)


def token_usage(protocol: str, usage: dict) -> dict[str, int]:
    if not isinstance(usage, dict):
        raise ValueError("missing_usage")

    def count(key, source=usage, default=None):
        if source is None:
            source = {}
        if not isinstance(source, dict):
            raise ValueError("invalid_usage")
        value = source.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("invalid_usage")
        return value

    if protocol == "chat/completions":
        total = count("prompt_tokens")
        cached = count("cached_tokens", usage.get("prompt_tokens_details"), 0)
        output = count("completion_tokens")
        written = 0
    elif protocol == "messages":
        total = count("input_tokens")
        cached = count("cache_read_input_tokens", default=0)
        written = count("cache_creation_input_tokens", default=0)
        # Messages reports uncached input separately from both cache buckets.
        total += cached + written
        output = count("output_tokens")
    else:
        total = count("input_tokens")
        cached = count("cached_tokens", usage.get("input_tokens_details"), 0)
        output = count("output_tokens")
        written = 0
    if cached + written > total:
        raise ValueError("invalid_usage")
    if protocol != "messages" and "total_tokens" in usage:
        reported_total = count("total_tokens")
        details_key = "completion_tokens_details" if protocol == "chat/completions" else "output_tokens_details"
        reasoning = count("reasoning_tokens", usage.get(details_key), 0)
        # Some converted tool responses exclude reasoning from completion_tokens.
        # Add it only when the independently reported total proves exclusion.
        if reported_total == total + output + reasoning:
            output += reasoning
        elif reported_total != total + output:
            raise ValueError("invalid_usage")
    hourly = count("ephemeral_1h_input_tokens", usage.get("cache_creation"), 0)
    if hourly > written:
        raise ValueError("invalid_cache_creation_usage")
    result = dict(zip(TOKEN_MODES, (total - cached - written, cached, written - hourly, output), strict=True))
    if hourly:
        result["cache_write_1h_tokens"] = hourly
    return result


def charges(rates: dict, units: dict, *, divisor=Decimal(1)) -> tuple[Decimal, Decimal]:
    retail = sum((Decimal(rates[k]["retail"]) * Decimal(v) / divisor for k, v in units.items()), Decimal(0))
    cost = sum((Decimal(rates[k]["cost"]) * Decimal(v) / divisor for k, v in units.items()), Decimal(0))
    return retail.quantize(Decimal(".01"), rounding=ROUND_HALF_UP), cost.quantize(
        Decimal(".000000000000000001"), rounding=ROUND_HALF_UP
    )


async def settle_actual(
    db: AsyncSession,
    generation: Generation,
    units: dict,
    *,
    provider_cost: Decimal | None = None,
    provider_cost_hold_rub: Decimal = Decimal("0.00"),
) -> None:
    """Append only compensations, once, using the immutable accepted price schedule.

    Caller holds the generation lock. New work is charged only after
    confirmed usage; old reservations and late refunds settle idempotently.
    """
    await db.execute(
        select(Generation)
        .where(Generation.id == generation.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if generation.billing_status in {"not_chargeable", "waived"}:
        raise HTTPException(409, "generation_not_chargeable")
    if generation.actual_charge_rub is not None:
        if generation.usage_snapshot is not None and generation.usage_snapshot != units:
            raise HTTPException(409, "settlement_usage_conflict")
        if provider_cost is not None and generation.actual_provider_cost_usdt != provider_cost:
            raise HTTPException(409, "settlement_cost_conflict")
        return
    snapshot = generation.request_payload or {}
    if snapshot.get("usage_reconciliation_pending") and generation.client_finalized_at is not None:
        finalized = generation.client_finalized_at
        if finalized.tzinfo is None:
            finalized = finalized.replace(tzinfo=UTC)
        seconds = snapshot.get(
            "native_usage_reconciliation_seconds", get_settings().native_usage_reconciliation_seconds
        )
        if finalized + timedelta(seconds=seconds) <= utc_now():
            raise HTTPException(409, "usage_reconciliation_deadline_expired")
    # A cost-only review may precede recovered usage. Never overwrite that
    # confirmed procurement amount with a second estimate from accepted rates.
    if provider_cost is None and generation.actual_provider_cost_usdt is not None:
        provider_cost = generation.actual_provider_cost_usdt
    divisor = MILLION if snapshot.get("protocol") in {"responses", "chat/completions", "messages"} else Decimal(1)
    charge, cost = charges(snapshot["rates"], units, divisor=divisor)
    if provider_cost is not None:
        if not provider_cost.is_finite() or provider_cost < 0:
            raise ValueError("invalid_provider_cost")
        cost = provider_cost
    covered = (cost * generation.rub_per_usdt_snapshot).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    await settle_generation_reserves(db, generation)
    await settle_confirmed_partner_charge(db, generation, charge)
    partner = await lock_partner_for_update(db, generation.partner_id)
    hold_rub = Decimal(provider_cost_hold_rub)
    if not hold_rub.is_finite() or hold_rub < 0:
        raise ValueError("invalid_provider_cost_hold")
    current_coverage_net = Decimal(
        await db.scalar(
            select(func.coalesce(func.sum(CoverageLedgerEntry.amount_rub), 0)).where(
                CoverageLedgerEntry.generation_id == generation.id,
                CoverageLedgerEntry.partner_id == generation.partner_id,
            )
        )
        or 0
    )
    provider_adjustment = -covered - hold_rub - current_coverage_net
    await apply_cost_coverage_change(
        db,
        partner,
        provider_adjustment,
        "provider_usage_adjustment",
        f"provider-usage:{generation.id}",
        generation.id,
        "Actual procurement settlement",
    )
    generation.actual_charge_rub = charge
    generation.billing_status = "charged"
    generation.actual_provider_cost_usdt = cost
    generation.usage_snapshot = units
    if "usage_reconciliation_pending" in snapshot:
        generation.request_payload = {**snapshot, "usage_reconciliation_pending": False}
    await db.flush()
