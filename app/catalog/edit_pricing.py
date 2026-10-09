"""Cost-plus pricing for the explicitly configured Seedance 2.5 edit mode.

The edit procurement rows are separate from default/reference tariffs. Only the
accepted quote is used for settlement; changing FX, markup or rows later cannot
reprice a generation that already exists.
"""

from decimal import Decimal

from fastapi import HTTPException

from app.infrastructure.config import get_settings

EDIT_MODEL = "seedance-2.5"
EDIT_PRICE_MODE = "edit"


def configured_edit_markup(model_slug: str) -> Decimal | None:
    if model_slug != EDIT_MODEL:
        return None
    return get_settings().seedance_25_edit_markup_rub_per_second


def edit_retail_rate(cost_usdt: Decimal, fx: Decimal, markup: Decimal) -> Decimal:
    """Keep unit precision; charges() rounds the final partner total to kopecks."""
    if (
        not all(isinstance(value, Decimal) and value.is_finite() for value in (cost_usdt, fx, markup))
        or cost_usdt <= 0
        or fx <= 0
        or markup < 0
    ):
        raise HTTPException(503, "provider_temporarily_unavailable")
    return cost_usdt * fx + markup


def quote_video_edit(body: dict, prices, *, fx: Decimal) -> dict[str, str] | None:
    """None preserves existing pricing for reference, auto, text and other models."""
    markup = configured_edit_markup(body["model"])
    if markup is None or body.get("omni_reference_task_type") != "edit":
        return None
    resolution = body.get("resolution", "720p")
    price = next(
        (
            row for row in prices
            if row.mode == EDIT_PRICE_MODE and row.resolution == resolution and row.billing_unit == "second"
        ),
        None,
    )
    if price is None:
        # Missing edit procurement is a rollout/data error, not permission to
        # silently use a stale default rate or another resolution.
        raise HTTPException(503, "provider_temporarily_unavailable")
    return {
        "retail": str(edit_retail_rate(price.provider_cost_usdt, fx, markup)),
        "cost": str(price.provider_cost_usdt),
    }
