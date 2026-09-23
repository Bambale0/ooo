"""Reserve before submission; synchronous requests are never blindly replayed."""

import hashlib
import json
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException
from sqlalchemy import select

from app.api.dependencies import PartnerAuth
from app.billing.service import (
    apply_cost_coverage_change,
    apply_partner_balance_change,
    lock_partner_for_update,
    require_sufficient_balance,
)
from app.catalog.models import Model, PartnerPrice
from app.contracts.registry import OBSERVATIONS, TEXT_PROTOCOLS, image_tier, normalized_video, video_reserve_seconds
from app.generations.models import Generation
from app.inference.accounting import MILLION, TOKEN_MODES, charges
from app.infrastructure.config import get_settings
from app.providers.models import ProviderAttempt
from app.providers.service import get_active_provider_credential


def fingerprint(protocol: str, body: dict, files_digest: str = "") -> str:
    return hashlib.sha256(
        (protocol + "\n" + json.dumps(body, sort_keys=True, separators=(",", ":")) + files_digest).encode()
    ).hexdigest()


async def reserve(db, auth: PartnerAuth, protocol: str, body: dict, idempotency_key: str, *, files_digest=""):
    partner = await lock_partner_for_update(db, auth.partner.id)
    if partner.status != "active":
        raise HTTPException(403, "partner_not_active")
    request_hash = fingerprint(protocol, body, files_digest)
    existing = (
        await db.execute(
            select(Generation).where(
                Generation.partner_id == partner.id,
                Generation.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing:
        if (existing.request_payload or {}).get("request_hash") != request_hash:
            raise HTTPException(409, "idempotency_conflict")
        return existing, None
    model = (
        await db.execute(select(Model).where(Model.slug == body["model"], Model.status == "production"))
    ).scalar_one_or_none()
    if model is None:
        raise HTTPException(404, "model_not_available")
    credential = await get_active_provider_credential(db, partner.id, "argolink")
    if credential is None:
        raise HTTPException(503, "provider_temporarily_unavailable")
    prices = list((await db.execute(select(PartnerPrice).where(PartnerPrice.model_id == model.id))).scalars())
    from app.billing.fx import current_fx
    from app.billing.fx import snapshot as fx_snapshot

    fx_data = await current_fx(db)
    rates, units, resolution = quote(protocol, body, prices, fx=fx_data["rate"])
    from app.generations.service import has_provider_capability

    for key in rates:
        if key == "input_reserve":
            continue
        mode = key if protocol in TEXT_PROTOCOLS else "default"
        tier = "default" if protocol in TEXT_PROTOCOLS else key if protocol.startswith("images/") else resolution
        if not await has_provider_capability(db, model.id, mode, tier):
            raise HTTPException(409, "capability_mismatch")
    divisor = MILLION if protocol in TEXT_PROTOCOLS else Decimal(1)
    charge, cost = charges(rates, units, divisor=divisor)
    fx = fx_data["rate"]
    coverage = (cost * fx).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    if charge < coverage:
        raise HTTPException(503, "provider_temporarily_unavailable")
    await require_sufficient_balance(partner, charge)
    from app.billing.capital import require_current_capital

    await require_current_capital(db, cost)
    # Native video requests are durable queue work. Sync inference has a durable
    # submission intent; workers must not turn a disconnected call into a second job.
    video = protocol == "videos/generations"
    snapshot = {
        "fx": fx_snapshot(fx_data),
        "protocol": protocol,
        "request_hash": request_hash,
        "rates": rates,
        "reserved_units": units,
    }
    if video:
        snapshot["native_body"] = body
    generation = Generation(
        partner_id=partner.id,
        model_id=model.id,
        model_slug=model.slug,
        mode=protocol,
        resolution=resolution,
        duration_seconds=units.get("seconds", 1),
        status="queued" if video else "submitting",
        idempotency_key=idempotency_key,
        partner_price_rub=charge,
        provider_cost_usdt_snapshot=cost,
        rub_per_usdt_snapshot=fx,
        provider_cost_reserve_rub=coverage,
        prompt="",
        request_payload=snapshot,
        webhook_url_snapshot=auth.api_key.webhook_url,
        webhook_secret_encrypted_snapshot=auth.api_key.webhook_secret_encrypted,
    )
    db.add(generation)
    await db.flush()
    await apply_partner_balance_change(
        db,
        partner,
        -charge,
        "generation_reserve",
        f"generation-reserve:{generation.id}",
        generation.id,
        "Native inference reserve",
        allow_negative=False,
    )
    await apply_cost_coverage_change(
        db,
        partner,
        -coverage,
        "provider_cost_reserve",
        f"provider-cost-reserve:{generation.id}",
        generation.id,
        "Native inference procurement reserve",
        allow_negative=True,
    )
    attempt = None
    if not video:
        attempt = ProviderAttempt(
            generation_id=generation.id, provider="argolink", credential_id=credential.id, status="submitting"
        )
        db.add(attempt)
    await db.commit()
    return generation, attempt


def quote(protocol, body, prices, *, fx=None):
    fx = fx if fx is not None else get_settings().rub_per_usdt
    rates = {}

    def add(key, mode, resolution, unit):
        price = next(
            (p for p in prices if p.mode == mode and p.resolution == resolution and p.billing_unit == unit), None
        )
        if price is None:
            raise HTTPException(503, "provider_temporarily_unavailable")
        # Check every rate individually; expensive cached/written tokens cannot
        # silently be sold below procurement just because the whole quote is positive.
        if price.provider_cost_usdt <= 0 or price.price_rub < price.provider_cost_usdt * fx:
            raise HTTPException(503, "provider_temporarily_unavailable")
        observed = OBSERVATIONS["manual_procurement_review"].get(body["model"])
        if (
            protocol == "images/edits"
            and observed
            and price.provider_cost_usdt < Decimal(observed["observed_default_edit_usd"])
        ):
            raise HTTPException(503, "provider_temporarily_unavailable")
        rates[key] = {"retail": str(price.price_rub), "cost": str(price.provider_cost_usdt)}

    if protocol in TEXT_PROTOCOLS:

        def uses_hour_cache(value):
            if isinstance(value, dict):
                if isinstance(value.get("cache_control"), dict) and value["cache_control"].get("ttl") == "1h":
                    return True
                return any(uses_hour_cache(v) for v in value.values())
            return isinstance(value, list) and any(uses_hour_cache(v) for v in value)

        if uses_hour_cache(body):
            add("cache_write_1h_tokens", "cache_write_1h_tokens", "default", "million_tokens")
        for mode in TOKEN_MODES:
            add(mode, mode, "default", "million_tokens")
        # Reserve a conservative context allowance for opaque multimodal/tool inputs.
        # This is a financial hold, not a cap or a modification of the native body.
        maximum = max(
            (body[k] for k in ("max_tokens", "max_completion_tokens", "max_output_tokens") if k in body),
            default=2_097_152,
        )
        serialized = json.dumps(body)
        opaque = any(
            marker in serialized
            for marker in (
                "image_url",
                "input_image",
                "input_audio",
                "file_id",
                "previous_response_id",
                "web_search",
                "computer_use",
                "mcp",
                "container",
                "file_search",
            )
        )
        input_bound = max(2_097_152, len(serialized.encode())) if opaque else len(serialized.encode()) + 4096
        input_modes = [key for key in rates if key != "output_tokens"]
        most_expensive = max(input_modes, key=lambda k: Decimal(rates[k]["retail"]))
        max_cost = max(Decimal(rates[k]["cost"]) for k in input_modes)
        rates["input_reserve"] = {"retail": rates[most_expensive]["retail"], "cost": str(max_cost)}
        units = {"input_reserve": input_bound, "output_tokens": maximum}
        return rates, units, "default"
    if protocol.startswith("images/"):
        tiers = ("1K", "2K", "4K") if body["model"].startswith("gpt-image") else ("1K", "2K")
        for tier in tiers:
            add(tier, "default", tier, "generation")
        # GPT can choose output dimensions. Reserve the most expensive possible tier;
        # actual bytes determine the final tier for every returned image.
        tier = (
            max(tiers, key=lambda t: Decimal(rates[t]["retail"]))
            if body["model"].startswith("gpt-image")
            else image_tier(body)
        )
        return rates, {tier: body.get("n", 1)}, tier
    video = normalized_video(body)
    resolution = video.get("resolution", "720p")
    add("seconds", "default", resolution, "second")
    return rates, {"seconds": video_reserve_seconds(body)}, resolution
