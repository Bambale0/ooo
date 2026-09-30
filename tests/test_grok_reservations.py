from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.inference.accounting import token_usage
from app.inference.router import scrub
from app.inference.service import quote


def grok_prices():
    return [
        SimpleNamespace(
            mode=mode,
            resolution="default",
            billing_unit="million_tokens",
            price_rub=Decimal(price),
            provider_cost_usdt=Decimal(cost),
        )
        for mode, price, cost in [
            ("input_tokens", "200", "1.6"),
            ("cached_input_tokens", "30", ".24"),
            ("cache_write_tokens", "200", "1.6"),
            ("output_tokens", "600", "4.8"),
        ]
    ]


def test_local_search_function_does_not_reserve_opaque_context():
    body = {
        "model": "grok-4.5",
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": "Use web_search to find a file_id example."}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "parameters": {"type": "object", "properties": {"file_id": {"type": "string"}}},
                },
            }
        ],
    }
    _, units, _ = quote("chat/completions", body, grok_prices(), fx=Decimal("100"))
    assert units["input_reserve"] < 5000
    assert units["output_tokens"] == 8192


@pytest.mark.parametrize(
    "extra",
    [
        {"tools": [{"type": "web_search"}]},
        {"previous_response_id": "opaque-id"},
        {
            "messages": [
                {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]}
            ]
        },
    ],
)
def test_opaque_inputs_keep_full_conservative_reservation(extra):
    body = {"model": "grok-4.5", "max_tokens": 128, **extra}
    _, units, _ = quote("chat/completions", body, grok_prices(), fx=Decimal("100"))
    assert units["input_reserve"] == 2_097_152


def test_separate_tool_reasoning_is_billed_when_total_confirms_it():
    usage = {
        "prompt_tokens": 314,
        "completion_tokens": 11,
        "total_tokens": 459,
        "prompt_tokens_details": {"cached_tokens": 128},
        "completion_tokens_details": {"reasoning_tokens": 134},
    }
    assert token_usage("chat/completions", usage) == {
        "input_tokens": 186,
        "cached_input_tokens": 128,
        "cache_write_tokens": 0,
        "output_tokens": 145,
    }


def test_inclusive_reasoning_is_not_billed_twice():
    usage = {
        "prompt_tokens": 2156,
        "completion_tokens": 98,
        "total_tokens": 2254,
        "completion_tokens_details": {"reasoning_tokens": 96},
    }
    assert token_usage("chat/completions", usage)["output_tokens"] == 98


def test_inconsistent_total_is_not_silently_undercharged():
    with pytest.raises(ValueError, match="invalid_usage"):
        token_usage(
            "chat/completions",
            {
                "prompt_tokens": 314,
                "completion_tokens": 11,
                "total_tokens": 900,
                "completion_tokens_details": {"reasoning_tokens": 134},
            },
        )


def test_stream_envelope_reports_the_same_output_count_as_billing():
    event = {
        "choices": [],
        "usage": {
            "prompt_tokens": 314,
            "completion_tokens": 11,
            "total_tokens": 459,
            "completion_tokens_details": {"reasoning_tokens": 134},
        },
    }
    assert scrub(event, "public-request")["usage"]["completion_tokens"] == 145
    assert event["usage"]["completion_tokens"] == 11


def test_partial_stream_usage_is_preserved_until_complete():
    event = {"usage": {"completion_tokens": 11, "total_tokens": 459}}
    assert scrub(event, "public-request")["usage"] == event["usage"]
