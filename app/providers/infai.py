"""Read-only InfAI management integration. No inference or price-table writes."""

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from email.utils import parsedate_to_datetime
from typing import Annotated

import httpx
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from app.catalog.models import Model, PartnerPrice
from app.infrastructure.config import get_settings
from app.providers.http_client import get_provider_http_client

Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class InfaiError(Exception):
    def __init__(self, code: str, status: int = 502):
        self.code, self.status = code, status
        super().__init__(code)


class Account(BaseModel):
    id: int = Field(strict=True, gt=0)
    group: str
    quota: Decimal = Field(allow_inf_nan=False)
    used_quota: Money
    request_count: int = Field(strict=True, ge=0)
    status: int = Field(strict=True)


class GroupHealth(BaseModel):
    status: str
    has_samples: bool


class Groups(BaseModel):
    current_group: str
    data: dict[str, str]
    ratios: dict[str, Money]
    group_ids: dict[str, int] = Field(default_factory=dict)
    uptime: dict[str, GroupHealth] = Field(default_factory=dict)


class PriceRecord(BaseModel):
    model_config = {"allow_inf_nan": False}

    model_name: str = Field(min_length=1, max_length=300)
    quota_type: int = Field(strict=True, ge=0)
    model_ratio: Money | None = None
    model_price: Money | None = None
    completion_ratio: Money | None = None
    cache_ratio: Money | None = None
    cache_creation_ratio: Money | None = None
    cache_creation_5m_ratio: Money | None = None
    cache_creation_1h_ratio: Money | None = None
    image_ratio: Money | None = None
    audio_ratio: Money | None = None
    audio_completion_ratio: Money | None = None
    step_ratios: list[dict[str, Decimal]] = Field(default_factory=list)
    video_resolution_ratios: dict[str, Money] = Field(default_factory=dict)
    enable_groups: list[str]
    supported_endpoint_types: list[str] = Field(default_factory=list)


class Pricing(BaseModel):
    data: list[PriceRecord] = Field(max_length=10000)
    group_ratio: dict[str, Money]
    group_model_ratio: dict[str, dict[str, Money]] = Field(default_factory=dict)


def group_ratio(pricing: Pricing, group: str, model: str) -> Decimal | None:
    """Provider precedence: exact override, longest prefix wildcard, group rate."""
    overrides = pricing.group_model_ratio.get(group, {})
    if model in overrides:
        return overrides[model]
    prefixes = [key for key in overrides if key.endswith("*") and model.startswith(key[:-1])]
    if prefixes:
        return overrides[max(prefixes, key=len)]
    return pricing.group_ratio.get(group)


def ratio_source(pricing: Pricing, group: str, model: str) -> str:
    overrides = pricing.group_model_ratio.get(group, {})
    if model in overrides:
        return "model:" + model
    prefixes = [key for key in overrides if key.endswith("*") and model.startswith(key[:-1])]
    if prefixes:
        return "pattern:" + max(prefixes, key=len)
    return "group" if group in pricing.group_ratio else "unavailable"


def reject_constant(value: str):
    raise ValueError("non-finite JSON number")


def retry_delay(value: str, attempt: int) -> float:
    if value.isascii() and value.isdigit():
        return float(value)
    if value:
        try:
            target = parsedate_to_datetime(value)
            return max(0, (target - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            pass
    return 2**attempt


def quote(record: PriceRecord, group: str, ratio: Decimal | None) -> dict:
    rates = None
    with localcontext() as context:
        context.prec = 50
        if (
            record.quota_type == 0
            and record.model_ratio is not None
            and record.completion_ratio is not None
            and ratio is not None
            and not record.model_name.startswith("gemini-omni-flash")
        ):
            base = Decimal(2) * record.model_ratio * ratio
            rates = {"input": base, "output": base * record.completion_ratio}
            for label, field in {
                "cached_input": "cache_ratio",
                "cache_write": "cache_creation_ratio",
                "cache_write_5m": "cache_creation_5m_ratio",
                "cache_write_1h": "cache_creation_1h_ratio",
                "image_input": "image_ratio",
            }.items():
                value = getattr(record, field)
                if value is not None:
                    rates[label] = base * value
            for label, field in {"audio_input": "audio_ratio", "audio_output": "audio_completion_ratio"}.items():
                value = getattr(record, field)
                if value is not None:
                    rates[label] = value * ratio
        return {
            "group": group,
            "ratio": ratio,
            "base_text_rates_usd_per_million": rates,
            "provider_base_price_usd": record.model_price * ratio
            if record.model_price is not None and ratio is not None
            else None,
            # Media prices can be credits, durations, pixels, or model-specific multipliers.
            "requires_request_specific_quote": True,
        }


class InfaiClient:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client

    async def _get(self, path: str, headers: dict) -> dict:
        client = self.client or get_provider_http_client("infai")
        for attempt in range(3):
            try:
                async with client.stream("GET", path, headers=headers, follow_redirects=False) as response:
                    if response.status_code in {401, 403}:
                        raise InfaiError("infai_auth_failed")
                    if response.is_redirect:
                        raise InfaiError("infai_unexpected_redirect")
                    if response.status_code == 429 or response.status_code >= 500:
                        delay = retry_delay(response.headers.get("retry-after", ""), attempt)
                        if attempt == 2 or delay > 2:
                            raise InfaiError("infai_temporarily_unavailable", 503)
                    elif response.status_code != 200:
                        raise InfaiError("infai_rejected_request")
                    else:
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                                raise InfaiError("infai_response_too_large")
                            body.extend(chunk)
                        try:
                            payload = json.loads(body, parse_float=Decimal, parse_constant=reject_constant)
                        except (ValueError, UnicodeError):
                            raise InfaiError("infai_invalid_response") from None
                        if not isinstance(payload, dict):
                            raise InfaiError("infai_invalid_response")
                        if payload.get("success") is not True:
                            raise InfaiError("infai_rejected_request")
                        return payload
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == 2:
                    raise InfaiError("infai_temporarily_unavailable", 503) from None
                delay = 2**attempt
            except httpx.HTTPError:
                raise InfaiError("infai_temporarily_unavailable", 503) from None
            await asyncio.sleep(delay)
        raise InfaiError("infai_temporarily_unavailable", 503)

    async def snapshot(self) -> dict:
        settings = get_settings()
        if not settings.infai_system_token or not settings.infai_user_id:
            raise InfaiError("infai_not_configured", 503)
        headers = {
            "Authorization": "Bearer " + settings.infai_system_token.get_secret_value(),
            "New-Api-User": str(settings.infai_user_id),
        }
        # Inspect every response; failed authentication never becomes an empty catalog.
        results = await asyncio.gather(
            self._get("/api/user/self", headers),
            self._get("/api/user/models", headers),
            self._get("/api/user/self/groups", headers),
            self._get("/api/pricing_new", headers),
            self._get("/api/status", {}),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                raise result
        try:
            account = Account.model_validate(results[0]["data"])
            available = results[1]["data"]
            if not isinstance(available, list) or not all(isinstance(name, str) and name for name in available):
                raise InfaiError("infai_invalid_response")
            if account.id != settings.infai_user_id:
                raise InfaiError("infai_account_mismatch")
            groups = Groups.model_validate(results[2]["data"])
            pricing = Pricing.model_validate(results[3])
            unit = results[4]["data"]["quota_per_unit"]
            if (
                isinstance(unit, bool)
                or not isinstance(unit, (int, Decimal))
                or not Decimal(unit).is_finite()
                or unit <= 0
            ):
                raise InfaiError("infai_invalid_quota_unit")
        except (KeyError, TypeError, ValidationError):
            raise InfaiError("infai_invalid_response") from None
        priced = {row.model_name: row for row in pricing.data}
        if len(priced) != len(pricing.data):
            raise InfaiError("infai_duplicate_model")
        available = set(available)
        models = []
        for name in sorted(available | priced.keys()):
            record = priced.get(name)
            models.append(
                {
                    "id": name,
                    "listed_for_account": name in available,
                    "pricing": record.model_dump() if record else None,
                    "group_quotes": [
                        {
                            **quote(record, group, group_ratio(pricing, group, name)),
                            "base_group_ratio": pricing.group_ratio.get(group),
                            "account_group_ratio": groups.ratios.get(group),
                            "ratio_source": ratio_source(pricing, group, name),
                        }
                        for group in record.enable_groups
                        if group in groups.data
                    ]
                    if record
                    else [],
                }
            )
        with localcontext() as context:
            context.prec = 50
            account_data = {
                **account.model_dump(),
                "quota_per_usd": Decimal(unit),
                "balance_usd": account.quota / Decimal(unit),
                "used_usd": account.used_quota / Decimal(unit),
            }
        return {
            "provider": "infai",
            "fetched_at": datetime.now(UTC).isoformat(),
            "account": account_data,
            "groups": groups.model_dump(),
            "models": models,
            "group_model_overrides": pricing.group_model_ratio,
            "counts": {
                "account_models": len(available),
                "priced_models": len(priced),
                "unpriced_account_models": len(available - priced.keys()),
            },
            "generation_routing_enabled": False,
            "retail_prices_changed": False,
        }


async def catalog_with_retail(db) -> dict:
    result = await InfaiClient().snapshot()
    local = {row.slug: row for row in (await db.execute(select(Model))).scalars()}
    prices = {}
    for row in (await db.execute(select(PartnerPrice))).scalars():
        prices.setdefault(row.model_id, []).append(
            {
                "mode": row.mode,
                "resolution": row.resolution,
                "billing_unit": row.billing_unit,
                "price_rub": row.price_rub,
            }
        )
    for row in result["models"]:
        model = local.get(row["id"])
        row["local_match"] = {"slug": model.slug, "status": model.status} if model else None
        row["retail_variants"] = prices.get(model.id, []) if model else []
        row["retail_policy"] = "preserve_existing" if model else "requires_review"
    result["counts"]["exact_local_matches"] = sum(row["local_match"] is not None for row in result["models"])
    return result
