"""Protocol terminal markers, not usage arrival, finalize native SSE requests."""

import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from test_native_inference import setup

from app.billing.models import LedgerEntry
from app.generations.models import Generation
from app.inference.streams import SSEDecoder, UsageCollector, event_data
from app.providers.models import ProviderAttempt


def data(value):
    return "data: " + json.dumps(value) + "\n\n"


EARLY = data({"usage": {"prompt_tokens": 10, "completion_tokens": 0}})
CONTENT = data({"choices": [{"delta": {"content": "Full answer"}}]})
FINAL_USAGE = data({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 20}})
DONE = "data: [DONE]\n\n"
BODY = {"model": "gpt-5.4", "messages": [{"role": "user", "content": "hi"}], "stream": True}


def test_usage_is_not_chat_completion():
    collector = UsageCollector("chat/completions")
    collector.accept(event_data(EARLY.strip()))
    assert not collector.complete
    collector.accept(event_data(DONE.strip()))
    assert collector.complete


@pytest.mark.parametrize("width", [1, 2, 7, 1000])
def test_done_is_recognized_across_crlf_chunk_boundaries(width):
    decoder, collector = SSEDecoder(), UsageCollector("chat/completions")
    payload = (EARLY + CONTENT + FINAL_USAGE + DONE).replace("\n", "\r\n")
    for offset in range(0, len(payload), width):
        for event in decoder.feed(payload[offset:offset + width]):
            collector.accept(event_data(event))
            if "[DONE]" not in event:
                assert not collector.complete
    assert collector.complete
    assert collector.usage == {"prompt_tokens": 10, "completion_tokens": 20}


async def request_stream(client, db_session, monkeypatch, payload, *, width=100000, error=False):
    calls = []

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for offset in range(0, len(payload), width):
                yield payload[offset:offset + width].encode()
            if error:
                raise httpx.ReadError("private upstream failure")

    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=Chunks(), headers={"Content-Type": "text/event-stream"})

    partner, headers, upstream = await setup(db_session, monkeypatch, handler)
    first = await client.post("/v1/chat/completions", headers=headers, json=BODY)
    replay = await client.post("/v1/chat/completions", headers=headers, json=BODY)
    assert first.status_code == replay.status_code == 200
    assert first.content == replay.content
    assert len(calls) == 1
    await upstream.aclose()
    generation = await db_session.scalar(select(Generation))
    attempt = await db_session.scalar(select(ProviderAttempt))
    return first, partner, generation, attempt


@pytest.mark.parametrize("width", [1, 7, 100000])
async def test_early_usage_does_not_truncate_content_or_charge_partial_usage(client, db_session, monkeypatch, width):
    result, partner, generation, attempt = await request_stream(
        client, db_session, monkeypatch, EARLY + CONTENT + FINAL_USAGE + DONE, width=width
    )
    assert "Full answer" in result.text
    assert result.text.count("[DONE]") == 1
    assert "stream_interrupted" not in result.text
    assert generation.status == "completed"
    assert generation.actual_charge_rub == Decimal(".03")
    assert partner.balance_rub == Decimal("999999.97")
    assert attempt.usage_snapshot["output_tokens"] == 20
    entries = list(await db_session.scalars(select(LedgerEntry)))
    assert len(entries) == 1


@pytest.mark.parametrize("ending,error", [("", False), ("", True), (data({"error": {"message": "private"}}), False)])
async def test_usage_followed_by_interruption_never_charges(client, db_session, monkeypatch, ending, error):
    result, partner, generation, attempt = await request_stream(
        client, db_session, monkeypatch, CONTENT + FINAL_USAGE + ending, error=error
    )
    assert "stream_interrupted" in result.text
    assert "[DONE]" not in result.text
    assert "private" not in result.text
    assert generation.status == "failed" and generation.billing_status == "not_chargeable"
    assert generation.actual_charge_rub is None
    assert partner.balance_rub == Decimal("1000000")
    assert attempt.cost_status == "unknown"
    assert not list(await db_session.scalars(select(LedgerEntry)))


async def test_done_without_usage_preserves_unbilled_result_and_replay(client, db_session, monkeypatch):
    result, partner, generation, attempt = await request_stream(client, db_session, monkeypatch, CONTENT + DONE)
    assert "Full answer" in result.text and result.text.count("[DONE]") == 1
    assert "stream_interrupted" not in result.text
    assert generation.status == "completed" and generation.actual_charge_rub is None
    assert generation.request_payload["usage_reconciliation_pending"] is True
    assert attempt.cost_status == "unknown"
    assert partner.balance_rub == Decimal("1000000")
    assert not list(await db_session.scalars(select(LedgerEntry)))


@pytest.mark.parametrize("usage", [None, {"prompt_tokens": 10}, {"prompt_tokens": -1, "completion_tokens": 20}])
async def test_terminal_with_invalid_usage_does_not_invent_charge(client, db_session, monkeypatch, usage):
    result, partner, generation, attempt = await request_stream(
        client, db_session, monkeypatch, CONTENT + data({"usage": usage}) + DONE
    )
    assert result.text.endswith(DONE) and "stream_interrupted" not in result.text
    assert generation.status == "completed" and generation.actual_charge_rub is None
    assert generation.request_payload["usage_reconciliation_pending"] is True


async def test_usage_after_done_is_not_billed(client, db_session, monkeypatch):
    result, partner, generation, attempt = await request_stream(
        client, db_session, monkeypatch, CONTENT + DONE + FINAL_USAGE
    )
    assert result.text.endswith(DONE) and "completion_tokens" not in result.text
    assert generation.actual_charge_rub is None
    assert generation.request_payload["usage_reconciliation_pending"] is True


@pytest.mark.parametrize("protocol,terminal", [("responses", "response.completed"), ("messages", "message_stop")])
def test_other_protocols_still_require_their_own_terminal(protocol, terminal):
    collector = UsageCollector(protocol)
    collector.accept({"usage": {"input_tokens": 10, "output_tokens": 20}})
    collector.accept(event_data(DONE.strip()))
    assert not collector.complete
    collector.accept({"type": terminal})
    assert collector.complete


@pytest.mark.parametrize("finish_result", [True, False, "error"])
async def test_terminal_is_withheld_until_persistence_and_failure_replay_omits_it(monkeypatch, finish_result):
    from types import SimpleNamespace

    from app.inference.router import stream_result

    persisted, failures = [], []

    async def finish(db, generation, attempt, units, *, response):
        persisted.append(response.body.decode())
        if finish_result == "error":
            raise ValueError("settlement_failed")
        return finish_result

    async def fail(db, generation, attempt, code, **kwargs):
        failures.append(kwargs["response"].body.decode())

    monkeypatch.setattr("app.inference.router.finish", finish)
    monkeypatch.setattr("app.inference.router.fail", fail)
    response = httpx.Response(200, content=CONTENT + FINAL_USAGE + DONE)
    stream = stream_result(None, SimpleNamespace(id="public-id"), SimpleNamespace(), response, "chat/completions")
    delivered = []
    async for frame in stream:
        if "[DONE]" in frame:
            assert persisted and finish_result is True
        delivered.append(frame)
    assert response.is_closed
    assert len(persisted) == 1 and persisted[0].endswith(DONE)
    if finish_result is True:
        assert "".join(delivered) == persisted[0]
        assert not failures
    else:
        assert "[DONE]" not in "".join(delivered)
        assert failures == ["".join(delivered)]
        assert "stream_interrupted" in failures[0]


async def test_cancelled_consumer_closes_provider_without_settlement(monkeypatch):
    from types import SimpleNamespace

    from app.inference.router import stream_result

    async def unexpected_finish(*args, **kwargs):
        pytest.fail("An interrupted consumer must not settle a partial stream")

    monkeypatch.setattr("app.inference.router.finish", unexpected_finish)
    response = httpx.Response(200, content=EARLY + CONTENT + FINAL_USAGE + DONE)
    stream = stream_result(None, SimpleNamespace(id="public-id"), SimpleNamespace(), response, "chat/completions")
    assert "usage" in await anext(stream)
    await stream.aclose()
    assert response.is_closed


async def test_duplicate_terminal_is_delivered_and_charged_once(client, db_session, monkeypatch):
    result, partner, generation, attempt = await request_stream(
        client, db_session, monkeypatch, CONTENT + FINAL_USAGE + DONE + DONE
    )
    assert result.text.count("[DONE]") == 1
    assert generation.actual_charge_rub == Decimal(".03")
    assert len(list(await db_session.scalars(select(LedgerEntry)))) == 1


@pytest.mark.parametrize("prefix", [
    "", ": heartbeat\n\n", data({"choices": []}), data({"choices": [None]}),
    data({"choices": [{"delta": {"role": "assistant"}}]}),
    data({"choices": [{"delta": {"content": 123}}]}),
    data({"choices": [{"delta": {"tool_calls": "bad"}}]}),
    data({"choices": [{"delta": {"function_call": {"arguments": 123}}}]}),
])
@pytest.mark.parametrize("usage_frame", ["", FINAL_USAGE])
async def test_done_without_valid_result_fails_without_charge(client, db_session, monkeypatch, prefix, usage_frame):
    result, partner, generation, attempt = await request_stream(
        client, db_session, monkeypatch, prefix + usage_frame + DONE
    )
    assert "[DONE]" not in result.text and "stream_interrupted" in result.text
    assert generation.status == "failed" and generation.billing_status == "not_chargeable"
    assert generation.actual_charge_rub is None
    assert not generation.request_payload.get("usage_reconciliation_pending")
    assert not list(await db_session.scalars(select(LedgerEntry)))


@pytest.mark.parametrize("choice", [
    {"delta": {"tool_calls": [{"index": 0, "function": {"name": "lookup", "arguments": "{}"}}]}},
    {"delta": {"function_call": {"name": "lookup", "arguments": "{}"}}},
    {"delta": {"reasoning_content": "Thinking"}},
    {"delta": {"refusal": "Cannot help"}},
    {"delta": {}, "finish_reason": "stop"},
    {"delta": {"content": ""}, "finish_reason": "stop"},
])
async def test_nontext_or_empty_finished_choice_is_valid(client, db_session, monkeypatch, choice):
    result, partner, generation, attempt = await request_stream(
        client, db_session, monkeypatch, data({"choices": [choice]}) + FINAL_USAGE + DONE
    )
    assert result.text.endswith(DONE) and "stream_interrupted" not in result.text
    assert generation.status == "completed" and generation.actual_charge_rub == Decimal(".03")


@pytest.mark.parametrize("final_usage", [None, {}, {"prompt_tokens": 12}, "invalid"])
async def test_invalid_final_snapshot_does_not_reuse_early_usage(client, db_session, monkeypatch, final_usage):
    result, partner, generation, attempt = await request_stream(
        client, db_session, monkeypatch, CONTENT + EARLY + data({"usage": final_usage}) + DONE
    )
    assert result.text.endswith(DONE)
    assert generation.actual_charge_rub is None
    assert generation.request_payload["usage_reconciliation_pending"] is True
    assert not list(await db_session.scalars(select(LedgerEntry)))


async def test_usage_before_later_content_is_not_final_usage(client, db_session, monkeypatch):
    result, partner, generation, attempt = await request_stream(client, db_session, monkeypatch, EARLY + CONTENT + DONE)
    assert result.text.endswith(DONE)
    assert generation.actual_charge_rub is None
    assert generation.request_payload["usage_reconciliation_pending"] is True


@pytest.mark.parametrize("protocol", ["chat/completions", "responses"])
def test_cumulative_usage_snapshots_replace_instead_of_merge(protocol):
    collector = UsageCollector(protocol)
    collector.accept({"usage": {"input_tokens": 10, "output_tokens": 0}})
    collector.accept({"usage": {"input_tokens": 12}})
    assert collector.usage == {"input_tokens": 12}
    collector.accept({"usage": None})
    assert collector.usage == {}


def test_messages_usage_deltas_still_merge():
    collector = UsageCollector("messages")
    collector.accept({"message": {"usage": {"input_tokens": 10, "output_tokens": 0}}})
    collector.accept({"usage": {"output_tokens": 20}})
    assert collector.usage == {"input_tokens": 10, "output_tokens": 20}


@pytest.mark.parametrize("delta", [
    {"content": "more"}, {"reasoning_content": "more"}, {"refusal": "more"},
    {"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]},
    {"function_call": {"arguments": "{}"}},
    {"audio": {"data": "YQ=="}},
])
def test_output_delta_invalidates_earlier_usage_but_same_frame_usage_is_fresh(delta):
    collector = UsageCollector("chat/completions")
    collector.accept({"usage": {"prompt_tokens": 10, "completion_tokens": 0}})
    collector.accept({"choices": [{"delta": delta}]})
    assert collector.usage == {} and collector.result_ready
    collector.accept({"choices": [{"delta": delta}], "usage": {"prompt_tokens": 10, "completion_tokens": 20}})
    assert collector.usage == {"prompt_tokens": 10, "completion_tokens": 20}


@pytest.mark.parametrize("event", [
    {"choices": [{"delta": {"role": "assistant"}}]},
    {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    {"choices": [{"delta": {"content": ""}}]},
    {"choices": []}, None,
])
def test_metadata_and_finish_only_do_not_invalidate_usage(event):
    collector = UsageCollector("chat/completions")
    collector.accept({"usage": {"prompt_tokens": 10, "completion_tokens": 20}})
    collector.accept(event)
    assert collector.usage == {"prompt_tokens": 10, "completion_tokens": 20}


@pytest.mark.parametrize("protocol,terminal", [("responses", "response.completed"), ("messages", "message_stop")])
async def test_other_protocols_bare_terminal_does_not_preserve_nonexistent_result(monkeypatch, protocol, terminal):
    from types import SimpleNamespace

    from app.inference.router import stream_result

    failures = []

    async def unexpected_success(*args, **kwargs):
        pytest.fail("Bare terminal without usage or result must not complete")

    async def fail(db, generation, attempt, code, **kwargs):
        failures.append(kwargs["response"].body.decode())

    monkeypatch.setattr("app.inference.router.finish", unexpected_success)
    monkeypatch.setattr("app.inference.router.preserve_unbilled_result", unexpected_success)
    monkeypatch.setattr("app.inference.router.fail", fail)
    response = httpx.Response(200, content=data({"type": terminal}))
    delivered = "".join([frame async for frame in stream_result(
        None, SimpleNamespace(id="public-id"), SimpleNamespace(), response, protocol
    )])
    assert terminal not in delivered
    assert "stream_interrupted" in delivered
    assert failures == [delivered]
    assert response.is_closed
