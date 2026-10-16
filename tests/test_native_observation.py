import httpx
import pytest
from sqlalchemy import select
from test_native_inference import setup

from app.generations.models import Generation
from app.providers.models import ProviderOutcome


@pytest.mark.parametrize("status,outcome", [(502,"error"),(200,"success")])
async def test_native_terminal_outcome_is_observed_once(client, db_session, monkeypatch, status, outcome):
    partner, headers, upstream = await setup(db_session, monkeypatch,
        lambda request: httpx.Response(status, json={"output":[{"content":"valid result"}],
            "usage":{"input_tokens":2,"output_tokens":3}}))
    body={"model":"gpt-5.4", "input":"hi", "max_output_tokens":128}
    first=await client.post("/v1/responses", headers=headers, json=body)
    again=await client.post("/v1/responses", headers=headers, json=body)
    assert first.status_code == again.status_code
    observations=list(await db_session.scalars(select(ProviderOutcome)))
    assert len(observations)==1 and observations[0].outcome==outcome
    await upstream.aclose()


async def test_non_object_sse_frame_closes_without_charge(client, db_session, monkeypatch):
    partner, headers, upstream = await setup(db_session, monkeypatch,
        lambda request: httpx.Response(200, content='data: [1,2,3]\n\n', headers={"Content-Type":"text/event-stream"}))
    response=await client.post("/v1/responses", headers=headers,
        json={"model":"gpt-5.4", "input":"hi", "max_output_tokens":128, "stream":True})
    assert response.status_code==200 and "stream_interrupted" in response.text
    generation=await db_session.scalar(select(Generation))
    assert generation.status=="failed" and generation.billing_status=="not_chargeable"
    assert generation.actual_charge_rub is None
    await upstream.aclose()
