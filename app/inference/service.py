"""Reserve before submission; synchronous requests are never blindly replayed."""

import hashlib
import json
import logging
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
from app.catalog.procurement import supports_free_rate
from app.contracts.registry import (
    MODELS,
    OBSERVATIONS,
    TEXT_PROTOCOLS,
    image_tier,
    normalized_video,
    video_reserve_seconds,
)
from app.generations.models import Generation
from app.inference.accounting import MILLION, TOKEN_MODES, charges
from app.infrastructure.config import get_settings
from app.providers.models import ProviderAttempt
from app.providers.service import get_active_provider_credential

logger = logging.getLogger(__name__)


class InferenceAdmissionError(HTTPException):
    def __init__(self, status_code: int, detail: str, *, failure_stage: str):
        super().__init__(status_code=status_code, detail=detail)
        self.failure_stage = failure_stage


def _admission_error(exc: HTTPException, stage: str) -> InferenceAdmissionError:
    return InferenceAdmissionError(exc.status_code, str(exc.detail), failure_stage=stage)


def fingerprint(protocol: str, body: dict, files_digest: str = "") -> str:
    return hashlib.sha256(
        (protocol + "\n" + json.dumps(body, sort_keys=True, separators=(",", ":")) + files_digest).encode()
    ).hexdigest()


def has_opaque_input(value) -> bool:
    """Inspect request structure, not words in prompts or local function schemas."""
    if isinstance(value, list):
        return any(has_opaque_input(item) for item in value)
    if not isinstance(value, dict):
        return False
    if any(
        key in value
        for key in ("image_url", "input_image", "input_audio", "file_id", "previous_response_id", "container")
    ):
        return True
    kind = value.get("type", "")
    if isinstance(kind, str) and (
        kind in {"image", "document", "input_file", "audio", "file"}
        or kind.startswith(("web_search", "computer", "mcp", "file_search", "code_interpreter"))
    ):
        return True
    for key, child in value.items():
        if key == "tools" and isinstance(child, list):
            # These tools execute in the caller, not inside the provider context.
            if any(
                not (
                    isinstance(tool, dict)
                    and (tool.get("type") == "function" or ("input_schema" in tool and "type" not in tool))
                )
                for tool in child
            ):
                return True
            continue
        if has_opaque_input(child):
            return True
    return False


async def reserve(
    db, auth: PartnerAuth, protocol: str, body: dict, idempotency_key: str, *, files_digest="", trial_telegram_id=None
):
    partner = await lock_partner_for_update(db, auth.partner.id)
    if partner.status != "active":
        raise InferenceAdmissionError(403, "partner_not_active", failure_stage="partner_status")
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
            raise InferenceAdmissionError(409, "idempotency_conflict", failure_stage="idempotency")
        return existing, None
    from app.catalog.access import RESTRICTED_STATUS, has_model_grant

    model = (
        await db.execute(
            select(Model).where(
                Model.slug == body["model"],
                Model.status.in_(["production", RESTRICTED_STATUS]),
            )
        )
    ).scalar_one_or_none()
    if model is None:
        raise InferenceAdmissionError(404, "model_not_available", failure_stage="model_access")
    if model.status == RESTRICTED_STATUS and not await has_model_grant(db, model.id, partner.id):
        raise HTTPException(404, "model_not_available")
    credential = await get_active_provider_credential(db, partner.id, "argolink")
    if credential is None:
        raise InferenceAdmissionError(503, "provider_temporarily_unavailable", failure_stage="provider_credential")
    prices = list((await db.execute(select(PartnerPrice).where(PartnerPrice.model_id == model.id))).scalars())
    from app.billing.fx import current_fx
    from app.billing.fx import snapshot as fx_snapshot

    try:
        fx_data = await current_fx(db)
    except HTTPException as exc:
        raise _admission_error(exc, "fx_rate") from exc
    if trial_telegram_id is not None:
        from app.telegram.trials import claim_trial

        if protocol != "videos/generations" or partner.telegram_id != trial_telegram_id:
            raise InferenceAdmissionError(403, "trial_not_available", failure_stage="trial")
        try:
            await claim_trial(db, trial_telegram_id)
        except HTTPException as exc:
            raise _admission_error(exc, "trial") from exc
    try:
        rates, units, resolution = quote(
            protocol,
            body,
            prices,
            fx=fx_data["rate"],
            trial=trial_telegram_id is not None,
        )
    except HTTPException as exc:
        raise _admission_error(exc, "pricing") from exc
    if trial_telegram_id is not None:
        rates = {key: {**value, "retail": "0"} for key, value in rates.items()}
    from app.generations.service import has_provider_capability

    for key in rates:
        if key == "input_reserve":
            continue
        mode = key if protocol in TEXT_PROTOCOLS else "default"
        tier = "default" if protocol in TEXT_PROTOCOLS else key if protocol.startswith("images/") else resolution
        if not await has_provider_capability(db, model.id, mode, tier):
            raise InferenceAdmissionError(409, "capability_mismatch", failure_stage="capability")
    divisor = MILLION if protocol in TEXT_PROTOCOLS else Decimal(1)
    charge, cost = charges(rates, units, divisor=divisor)
    fx = fx_data["rate"]
    coverage = (cost * fx).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    if trial_telegram_id is None and charge < coverage:
        raise InferenceAdmissionError(503, "provider_temporarily_unavailable", failure_stage="pricing")
    try:
        await require_sufficient_balance(partner, charge)
    except HTTPException as exc:
        raise _admission_error(exc, "balance") from exc
    from app.billing.capital import require_provider_capital

    try:
        await require_provider_capital(db, cost, partner_id=partner.id)
    except HTTPException as exc:
        raise _admission_error(exc, "provider_capital") from exc
    # Native video requests are durable queue work. Sync inference has a durable
    # submission intent; workers must not turn a disconnected call into a second job.
    video = protocol == "videos/generations"
    snapshot = {
        "fx": fx_snapshot(fx_data),
        "protocol": protocol,
        "request_hash": request_hash,
        "rates": rates,
        "reserved_units": units,
        "api_key_id": auth.api_key.id,
    }
    if video:
        snapshot["native_body"] = body
    if trial_telegram_id is not None:
        snapshot["trial_telegram_id"] = trial_telegram_id
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
    from app.providers.circuit import require_admission

    try:
        await require_admission(db, generation.id, claim=not video)
    except HTTPException as exc:
        raise _admission_error(exc, "provider_circuit") from exc
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
    logger.info(
        "generation_reserved",
        extra={
            "trace_id": generation.id,
            "generation_id": generation.id,
            "partner_id": generation.partner_id,
            "api_key_id": auth.api_key.id,
            "model_id": generation.model_id,
            "attempt_id": attempt.id if attempt else None,
        },
    )
    return generation, attempt


def quote(protocol, body, prices, *, fx=None, trial=False):
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
        free_rate = supports_free_rate(body["model"], mode, resolution, unit)
        if (
            price.provider_cost_usdt < 0
            or price.price_rub < 0
            or (not free_rate and (price.provider_cost_usdt == 0 or price.price_rub == 0))
            or (not trial and price.price_rub < price.provider_cost_usdt * fx)
        ):
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
        if body.get("service_tier") in ("priority", "fast"):
            multiplier = Decimal(str(MODELS[body["model"]].get("fast_multiplier", 1)))
            rates = {
                key: {kind: str(Decimal(value) * multiplier) for kind, value in rate.items()}
                for key, rate in rates.items()
            }
        # Reserve a conservative context allowance for opaque multimodal/tool inputs.
        # This is a financial hold, not a cap or a modification of the native body.
        maximum = max(
            (body[k] for k in ("max_tokens", "max_completion_tokens", "max_output_tokens") if k in body),
            default=2_097_152,
        )
        serialized = json.dumps(body)
        opaque = has_opaque_input(body)
        input_bound = max(2_097_152, len(serialized.encode())) if opaque else len(serialized.encode()) + 4096
        input_modes = [key for key in rates if key != "output_tokens"]
        most_expensive = max(input_modes, key=lambda k: Decimal(rates[k]["retail"]))
        max_cost = max(Decimal(rates[k]["cost"]) for k in input_modes)
        rates["input_reserve"] = {"retail": rates[most_expensive]["retail"], "cost": str(max_cost)}
        units = {"input_reserve": input_bound, "output_tokens": maximum}
        return rates, units, "default"
    if protocol.startswith("images/"):
        tiers = tuple(tier["label"] for tier in MODELS[body["model"]]["procurement"]["tiers"])
        for tier in tiers:
            add(tier, "default", tier, "generation")
        # GPT can choose output dimensions. Equal partner prices still need the
        # highest procurement reserve; actual bytes determine the final tier.
        tier = (
            max(tiers, key=lambda t: (Decimal(rates[t]["retail"]), Decimal(rates[t]["cost"])))
            if body["model"].startswith("gpt-image")
            else image_tier(body)
        )
        return rates, {tier: body.get("n", 1)}, tier
    video = normalized_video(body)
    resolution = video.get("resolution", "768p" if body["model"] == "minimax-h3" else "720p")
    add("seconds", "default", resolution, "second")
    return rates, {"seconds": video_reserve_seconds(body)}, resolution
