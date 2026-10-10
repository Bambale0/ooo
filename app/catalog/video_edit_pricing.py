"""Opt-in pricing for Seedance 2.5 edit; never used during settlement.

The edit procurement row is separate from default pricing. Accepted generations
store exact Decimal rates and FX in their existing immutable request snapshot.
"""

from decimal import Decimal
from typing import Protocol

from fastapi import HTTPException

from app.infrastructure.config import get_settings

EDIT_MODEL = "seedance-2.5"
EDIT_MODE = "edit"
# ArgoLink Seedance 2.5: video input changes the supplier rate for *all*
# billable seconds, including output and reference seconds.
# This is procurement accounting, not a separate provider request mode.
VIDEO_INPUT_USD_PER_SECOND = {
    "480p": Decimal("0.0523"),
    "720p": Decimal("0.117"),
    "1080p": Decimal("0.289"),
}


def video_input_cost(model: str, body: dict, resolution: str) -> Decimal | None:
    if model == EDIT_MODEL and body.get("reference_videos"):
        return VIDEO_INPUT_USD_PER_SECOND[resolution]
    return None


class Price(Protocol):
    mode: str
    billing_unit: str
    price_rub: Decimal
    provider_cost_usdt: Decimal


def edit_markup(model: str) -> Decimal | None:
    if model != EDIT_MODEL:
        return None
    return get_settings().seedance_25_edit_markup_rub_per_second


def video_pricing_mode(body: dict) -> str:
    if (
        body.get("model") == EDIT_MODEL
        and edit_markup(body.get("model")) is not None
        and bool(body.get("reference_videos"))
    ):
        return EDIT_MODE
    return "default"


def retail_rate(model: str, price: Price, fx: Decimal) -> Decimal:
    markup = edit_markup(model)
    if markup is None or price.mode != EDIT_MODE or price.billing_unit != "second":
        return price.price_rub
    cost = price.provider_cost_usdt
    if not all(value.is_finite() for value in (cost, fx, markup)) or cost <= 0 or fx <= 0 or markup < 0:
        raise HTTPException(503, "provider_temporarily_unavailable")
    # Round only the final charge, not a rate multiplied by many billed seconds.
    return cost * fx + markup
