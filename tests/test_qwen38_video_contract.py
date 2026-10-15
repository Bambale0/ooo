"""Keep unsupported Qwen video inputs out of ArgoLink and financial state."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.billing.models import LedgerEntry
from app.generations.models import Generation
from test_native_inference import setup

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
