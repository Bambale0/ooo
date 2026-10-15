"""Keep unsupported Qwen video inputs out of ArgoLink and financial state."""

from decimal import Decimal

import pytest
from sqlalchemy import select
from test_native_inference import setup

from app.billing.models import LedgerEntry
from app.generations.models import Generation

MODEL = "qwen3.8-max"


@pytest.mark.parametrize(
    "video_part",
    [
        {
            "type": "video_url",
            "video_url": {"url": "https://example.org/sports.mp4"},
            "fps": 2,
        },
        {"type": "video", "video": ["https://example.org/frame1.jpg", "https://example.org/frame2.jpg"]},
    ],
)
async def test_qwen_unsupported_video_is_rejected_before_reserve(
    client, db_session, monkeypatch, video_part
):
    calls = []

    def handler(request):
        calls.append(request)
        raise AssertionError("ArgoLink does not support video input for this model")

    partner, headers, upstream = await setup(db_session, monkeypatch, handler, model=MODEL)
    request_body = {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": [video_part, {"type": "text", "text": "Summarize the clip"}],
            }
        ],
        "max_completion_tokens": 256,
    }
    try:
        response = await client.post("/v1/chat/completions", headers=headers, json=request_body)
        assert response.status_code == 422
        assert response.json()["detail"] == "invalid_request_contract"
        assert response.headers["x-validation-error"] == "video_input_unavailable"
        assert response.headers["x-request-id"]
        assert calls == []
        assert (await db_session.execute(select(Generation))).scalars().all() == []
        assert (await db_session.execute(select(LedgerEntry))).scalars().all() == []
        await db_session.refresh(partner)
        assert partner.balance_rub == Decimal("1000000")
    finally:
        await upstream.aclose()


@pytest.mark.parametrize(
    "protocol,payload",
    [
        (
            "chat/completions",
            {
                "model": MODEL,
                "messages": [{"role": "user", "content": [
                    {"type": "video_url", "video_url": {"url": "https://example.org/clip.mp4"}, "fps": 2}
                ]}],
            },
        ),
        (
            "responses",
            {
                "model": MODEL,
                "input": [{"role": "user", "content": [
                    {"type": "input_video", "video_url": "https://example.org/clip.mp4"}
                ]}],
            },
        ),
        (
            "messages",
            {
                "model": MODEL,
                "max_tokens": 128,
                "messages": [{"role": "user", "content": [
                    {"type": "video", "video": ["https://example.org/frame.jpg"]}
                ]}],
            },
        ),
    ],
)
def test_qwen_all_text_protocols_reject_video_shape_before_provider(protocol, payload):
    from app.contracts.registry import validate_request
    from app.contracts.validation_errors import validation_reason

    with pytest.raises(ValueError) as exc:
        validate_request(protocol, payload)
    assert validation_reason(exc.value) == "video_input_unavailable"


def test_qwen_text_image_and_tools_mentioning_video_remain_unchanged():
    from app.contracts.registry import validate_request

    body = {
        "model": MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this video-themed cover image"},
                {"type": "image_url", "image_url": {"url": "https://example.org/poster.jpg"}},
            ],
        }],
        "tools": [{
            "type": "function",
            "function": {
                "name": "lookup_video",
                "parameters": {"type": "object", "properties": {"video_url": {"type": "string"}}},
            },
        }],
        "max_completion_tokens": 256,
    }
    assert validate_request("chat/completions", body) == body
