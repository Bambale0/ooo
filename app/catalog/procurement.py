"""Reviewed exceptions to the otherwise strictly positive price schedule."""

from decimal import Decimal, InvalidOperation

from app.contracts.registry import MODELS

TOKEN_PRICE_FIELDS = {
    "input_tokens": "input_per_million",
    "output_tokens": "output_per_million",
    "cached_input_tokens": "cached_input_per_million",
    "cache_write_tokens": "cache_write_per_million",
}


def supports_free_rate(model_slug: str, mode: str, resolution: str, billing_unit: str) -> bool:
    """Allow zero only when this exact token dimension is explicitly reviewed as free."""
    entry = MODELS.get(model_slug)
    source = TOKEN_PRICE_FIELDS.get(mode)
    if (
        entry is None
        or entry["category"] != "chat"
        or resolution != "default"
        or billing_unit != "million_tokens"
        or source is None
    ):
        return False
    price = entry["procurement"]
    value = price.get(source)
    if price.get("billing_mode") != "token" or value is None or isinstance(value, bool):
        return False
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return False
    return amount.is_finite() and amount == 0
