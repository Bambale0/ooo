from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.service import (
    apply_cost_coverage_change,
    apply_partner_balance_change,
    lock_partner_for_update,
    settle_generation_reserves,
)
from app.generations.models import Generation

TOKEN_MODES = ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens")
MILLION = Decimal(1_000_000)


def token_usage(protocol: str, usage: dict) -> dict[str, int]:
    if not isinstance(usage, dict):
        raise ValueError("missing_usage")

    def count(key, source=usage, default=None):
        value = source.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("invalid_usage")
        return value

    if protocol == "chat/completions":
        total = count("prompt_tokens")
        cached = count("cached_tokens", usage.get("prompt_tokens_details") or {}, 0)
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
        cached = count("cached_tokens", usage.get("input_tokens_details") or {}, 0)
        output = count("output_tokens")
        written = 0
    if cached + written > total:
        raise ValueError("invalid_usage")
    if protocol != "messages" and "total_tokens" in usage:
        reported_total = count("total_tokens")
        details_key = "completion_tokens_details" if protocol == "chat/completions" else "output_tokens_details"
        reasoning = count("reasoning_tokens", usage.get(details_key) or {}, 0)
        # Some converted tool responses exclude reasoning from completion_tokens.
        # Add it only when the independently reported total proves exclusion.
        if reported_total == total + output + reasoning:
            output += reasoning
        elif reported_total != total + output:
            raise ValueError("invalid_usage")
    hourly = count("ephemeral_1h_input_tokens", usage.get("cache_creation") or {}, 0)
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


async def settle_actual(db: AsyncSession, generation: Generation, units: dict) -> None:
    """Append only compensations, once, using the immutable accepted price schedule.

    Caller holds the generation lock. A late successful job first restores the
    original reserve and then applies exactly one usage adjustment.
    """
    await db.execute(
        select(Generation)
        .where(Generation.id == generation.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if generation.actual_charge_rub is not None:
        return
    snapshot = generation.request_payload or {}
    divisor = MILLION if snapshot.get("protocol") in {"responses", "chat/completions", "messages"} else Decimal(1)
    charge, cost = charges(snapshot["rates"], units, divisor=divisor)
    covered = (cost * generation.rub_per_usdt_snapshot).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    await settle_generation_reserves(db, generation)
    partner = await lock_partner_for_update(db, generation.partner_id)
    await apply_partner_balance_change(
        db,
        partner,
        generation.partner_price_rub - charge,
        "generation_usage_adjustment",
        f"generation-usage:{generation.id}",
        generation.id,
        "Actual usage settlement",
    )
    await apply_cost_coverage_change(
        db,
        partner,
        generation.provider_cost_reserve_rub - covered,
        "provider_usage_adjustment",
        f"provider-usage:{generation.id}",
        generation.id,
        "Actual procurement settlement",
    )
    generation.actual_charge_rub = charge
    generation.actual_provider_cost_usdt = cost
    generation.usage_snapshot = units
    await db.flush()
