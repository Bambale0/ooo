"""Qwen limits and compatible warnings without rewriting requests or spending."""

import copy
import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from test_native_inference import setup

from app.billing.models import LedgerEntry
from app.contracts.registry import request_warnings, validate_request
from app.generations.models import Generation
from app.providers.models import ProviderAttempt

MODEL = "qwen3.8-max"
TOKEN_KEYS = ("max_tokens", "max_completion_tokens", "max_output_tokens")


def request_body(protocol):
    if protocol == "responses":
        return {"model": MODEL, "input": "Hi"}
    body = {"model": MODEL, "messages": [{"role": "user", "content": "Hi"}]}
    if protocol == "messages":
        body["max_tokens"] = 512
    return body


@pytest.mark.parametrize("protocol", ["chat/completions", "responses", "messages"])
@pytest.mark.parametrize("key", TOKEN_KEYS)
@pytest.mark.parametrize("value", [1, 131_072, 131_073])
def test_qwen_output_cap_is_checked_for_each_alias(protocol, key, value):
    body = {**request_body(protocol), key: value}
    original = copy.deepcopy(body)
    if value > 131_072:
        with pytest.raises(ValueError, match=f"^invalid_{key}$"):
            validate_request(protocol, body)
    else:
        assert validate_request(protocol, body) == original
    assert body == original


@pytest.mark.parametrize("key", TOKEN_KEYS)
async def test_oversize_qwen_cap_fails_before_provider_or_financial_state(
    client, db_session, monkeypatch, key,
):
    def forbidden(request):
        raise AssertionError("Invalid cap must not reach provider")

    partner, headers, upstream = await setup(db_session, monkeypatch, forbidden, model=MODEL)
    try:
        result = await client.post(
            "/v1/chat/completions", headers=headers,
            json={**request_body("chat/completions"), key: 131_073},
        )
        assert result.status_code == 422
        assert result.json() == {"detail": "invalid_request_contract"}
        assert result.headers["x-validation-error"] == f"invalid_{key}"
        for entity in (Generation, ProviderAttempt, LedgerEntry):
            assert list((await db_session.scalars(select(entity))).all()) == []
        await db_session.refresh(partner)
        assert partner.balance_rub == Decimal("1000000")
    finally:
        await upstream.aclose()


def test_other_models_and_omitted_qwen_caps_keep_existing_contract():
    for protocol in ("chat/completions", "responses", "messages"):
        body = request_body(protocol)
        assert validate_request(protocol, body) == body
        body.update(model="gpt-5.4", max_tokens=131_073)
        assert validate_request(protocol, body) == body


@pytest.mark.parametrize("protocol", ["chat/completions", "responses", "messages"])
@pytest.mark.parametrize("controls", [
    {"enable_thinking": False}, {"enable_thinking": True}, {"thinking_budget": 10},
    {"reasoning_effort": "low"}, {"thinking": {"type": "disabled"}},
    {"reasoning": {"effort": "low"}},
])
def test_ignored_thinking_controls_remain_compatible_and_warn(protocol, controls):
    body = {**request_body(protocol), **controls}
    original = copy.deepcopy(body)
    assert validate_request(protocol, body) == original
    assert request_warnings(body) == ("thinking_controls_ignored",)
    assert body == original


def test_warning_does_not_match_prompt_or_tool_schemas_or_other_models():
    body = {
        **request_body("chat/completions"),
        "messages": [{"role": "user", "content": "enable_thinking thinking_budget service_tier"}],
        "tools": [{"type": "function", "function": {
            "name": "thinking",
            "parameters": {"type": "object", "properties": {"thinking_budget": {"type": "integer"}}},
        }}],
        "reasoning": {"summary": "auto"},
    }
    assert request_warnings(body) == ()
    assert request_warnings({**body, "model": "gpt-5.4", "reasoning_effort": "low", "service_tier": "priority"}) == ()


@pytest.mark.parametrize("stream", [False, True])
async def test_qwen_ignored_controls_warn_without_changing_payload_or_replay(
    client, db_session, monkeypatch, stream,
):
    calls = []
    usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}

    def handler(request):
        calls.append(json.loads(request.content))
        if stream:
            content = (
                'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
                + "data: " + json.dumps({"choices": [], "usage": usage}) + "\n\n"
                + "data: [DONE]\n\n"
            )
            return httpx.Response(200, text=content, headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "Hi"}}], "usage": usage,
        })

    partner, headers, upstream = await setup(db_session, monkeypatch, handler, model=MODEL)
    body = {
        **request_body("chat/completions"), "stream": stream, "max_completion_tokens": 512,
        "enable_thinking": False, "thinking_budget": 10, "reasoning_effort": "low", "service_tier": "priority",
    }
    original = copy.deepcopy(body)
    try:
        result = await client.post("/v1/chat/completions", headers=headers, json=body)
        assert result.status_code == 200
        assert result.headers["x-model-warning"] == "thinking_controls_ignored, service_tier_unsupported"
        repeat = await client.post("/v1/chat/completions", headers=headers, json=body)
        assert repeat.status_code == 200
        assert repeat.content == result.content
        assert repeat.headers["x-model-warning"] == result.headers["x-model-warning"]
        expected_wire = {**original, "stream_options": {"include_usage": True}} if stream else original
        assert calls == [expected_wire]
        assert body == original
    finally:
        await upstream.aclose()


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_public_docs_explain_qwen_limits_and_ignored_controls(client, lang):
    result = await client.get("/docs", params={"lang": lang})
    section = result.text.split('id="text-qwen-controls"', 1)[1].split('<h3', 1)[0]
    for term in ("131072", "enable_thinking", "thinking_budget", "reasoning_effort", "X-Model-Warning"):
        assert term in section
    assert ("всегда включён" if lang == "ru" else "always enabled") in section
    assert ("до появления ответа" if lang == "ru" else "before any answer") in section
