"""Qwen3.8-Max integration contract and native accounting regression."""

import json
from decimal import Decimal

import httpx
from sqlalchemy import select
from test_native_inference import setup

from app.api.text_reference import PUBLIC_TEXT_MODELS
from app.catalog.sync import variants
from app.contracts.registry import MODELS, validate_request
from app.generations.models import Generation

MODEL = "qwen3.8-max"
COSTS = {
    "input_tokens": Decimal("1.30"),
    "output_tokens": Decimal("3.90"),
    "cached_input_tokens": Decimal("0.1625"),
    "cache_write_tokens": Decimal("1.625"),
}


def test_qwen38_max_reviewed_contract_and_passthrough():
    entry = MODELS[MODEL]
    assert entry["category"] == "chat"
    assert entry["endpoint"] == "/v1/chat/completions"
    public = PUBLIC_TEXT_MODELS[MODEL]
    assert public == {
        "context_tokens": 1_000_000,
        "max_input_tokens": 983_616,
        "max_output_tokens": 131_072,
        "images": True,
        "thinking": "always",
    }
    actual = {(m, tier, unit): cost for m, tier, unit, cost in variants(entry)}
    assert actual == {(mode, "default", "million_tokens"): cost for mode, cost in COSTS.items()}

    native = {
        "model": MODEL,
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "Describe this chart"},
                {"type": "image_url", "image_url": {"url": "https://example.org/chart.png"}},
            ]}
        ],
        "max_completion_tokens": 256,
        "stream": False,
    }
    assert validate_request("chat/completions", native) == native
    responses = {"model": MODEL, "input": "Explain optimistic locking", "max_output_tokens": 256}
    assert validate_request("responses", responses) == responses
    messages = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Hi"}],
        "max_tokens": 256,
    }
    assert validate_request("messages", messages) == messages


async def test_qwen38_max_native_chat_completion_settles_actual_usage_once(client, db_session, monkeypatch):
    calls = []

    def upstream_handler(request):
        assert request.method == "POST" and request.url.path == "/v1/chat/completions"
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "private-qwen-provider-id",
                "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                "usage": {
                    "prompt_tokens": 125,
                    "prompt_tokens_details": {"cached_tokens": 25},
                    "completion_tokens": 80,
                    "total_tokens": 205,
                },
                "provider": "secret-provider",
            },
        )

    retail = {
        "input_tokens": Decimal("166.67"),
        "output_tokens": Decimal("500.00"),
        "cached_input_tokens": Decimal("20.83"),
        "cache_write_tokens": Decimal("208.33"),
    }
    rates = [
        (mode, "default", "million_tokens", retail[mode], COSTS[mode])
        for mode in ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_tokens")
    ]
    partner, headers, upstream = await setup(
        db_session, monkeypatch, upstream_handler, model=MODEL, rates=rates
    )
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Reply with OK."}],
        "max_completion_tokens": 512,
        "stream": False,
    }
    try:
        listed = await client.get("/v1/models")
        assert listed.status_code == 200
        assert MODEL in {item["id"] for item in listed.json()["data"]}

        response = await client.post("/v1/chat/completions", headers=headers, json=body)
        assert response.status_code == 200, response.text
        result = response.json()
        assert calls == [body]
        assert result["id"] != "private-qwen-provider-id"
        assert "provider" not in result

        generation = (
            await db_session.execute(select(Generation).where(Generation.model_slug == MODEL))
        ).scalar_one()
        assert generation.status == "completed"
        assert generation.actual_charge_rub == Decimal("0.06")
        assert generation.actual_provider_cost_usdt == Decimal("0.000446062500000000")
        assert generation.usage_snapshot == {
            "input_tokens": 100,
            "cached_input_tokens": 25,
            "cache_write_tokens": 0,
            "output_tokens": 80,
        }
        repeated = await client.post("/v1/chat/completions", headers=headers, json=body)
        assert repeated.status_code == 409
        assert calls == [body]
        await db_session.refresh(partner)
        assert partner.balance_rub == Decimal("999999.94")
    finally:
        await upstream.aclose()
