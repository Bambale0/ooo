import base64
import io
import json
from decimal import Decimal

import httpx
import pytest
from PIL import Image
from sqlalchemy import select

from app.accounts.models import ApiKey, Partner
from app.billing.models import LedgerEntry
from app.catalog.models import Model, PartnerPrice
from app.generations.models import Generation
from app.inference.accounting import token_usage
from app.infrastructure.config import get_settings
from app.infrastructure.security import encrypt_secret, hash_secret
from app.providers.argolink import ArgoLinkAdapter
from app.providers.models import ProviderCredential, ProviderModelCapability


async def setup(db, monkeypatch, handler, *, model="gpt-5.4", category="llm", rates=None):
    partner = Partner(
        telegram_id="native-1",
        company_name="Test",
        project_name="Test",
        balance_rub=Decimal("1000000"),
        cost_coverage_rub=Decimal("1000000"),
    )
    db.add(partner)
    db.add(Model(slug=model, name=model, modality=category, status="production"))
    await db.flush()
    m = (await db.execute(select(Model).where(Model.slug == model))).scalar_one()
    db.add(ApiKey(partner_id=partner.id, name="test", key_hash=hash_secret("partner-native-key"), key_prefix="test"))
    db.add(
        ProviderCredential(
            provider="argolink",
            label="test",
            partner_id=partner.id,
            key_hash=hash_secret("upstream"),
            key_prefix="upstream",
            encrypted_api_key=encrypt_secret("upstream", get_settings().provider_credentials_master_key),
        )
    )
    if rates is None:
        rates = [
            (k, "default", "million_tokens", Decimal("1000"), Decimal("1"))
            for k in ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens")
        ]
    for mode, res, unit, price, cost in rates:
        db.add(
            PartnerPrice(
                model_id=m.id, mode=mode, resolution=res, billing_unit=unit, price_rub=price, provider_cost_usdt=cost
            )
        )
    for mode, res, _unit, _price, _cost in rates:
        db.add(ProviderModelCapability(provider="argolink", model_id=m.id, mode=mode, resolution=res, is_active=True))
    await db.commit()
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://argolink.io")
    adapter = ArgoLinkAdapter(api_key="upstream", client=upstream)

    async def get_adapter(*a, **kw):
        return adapter

    monkeypatch.setattr("app.inference.router.get_partner_provider_adapter", get_adapter)
    headers = {"Authorization": "Bearer partner-native-key", "Idempotency-Key": "test-native-key"}
    return partner, headers, upstream


async def test_native_actual_usage_and_duplicate_never_resubmits(client, db_session, monkeypatch):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "private-provider-id",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}],
                "usage": {"input_tokens": 10, "output_tokens": 20, "cost": 44},
                "provider": "secret",
            },
        )

    partner, headers, upstream = await setup(db_session, monkeypatch, handler)
    body = {
        "model": "gpt-5.4",
        "input": "Hello",
        "max_output_tokens": 128,
        "tools": [{"type": "function", "name": "foo", "parameters": {"type": "object"}}],
    }
    response = await client.post("/v1/responses", json=body, headers=headers)
    assert response.status_code == 200
    assert calls == [body]
    result = response.json()
    assert result["id"] != "private-provider-id" and "provider" not in result and "cost" not in result["usage"]
    generation = await db_session.get(Generation, result["id"])
    assert generation.actual_charge_rub == Decimal(".03")
    assert generation.provider_cost_usdt_snapshot > generation.actual_provider_cost_usdt
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("999999.97")
    again = await client.post("/v1/responses", json=body, headers=headers)
    assert again.status_code == 409 and len(calls) == 1
    changed = await client.post("/v1/responses", json={**body, "input": "different"}, headers=headers)
    assert changed.status_code == 409 and changed.json()["detail"] == "idempotency_conflict"
    entries = list((await db_session.execute(select(LedgerEntry))).scalars())
    assert len(entries) == 2
    await upstream.aclose()


@pytest.mark.parametrize("status,held", [(402, False), (429, False), (408, True), (500, True)])
async def test_native_rejections_and_ambiguous_outcomes(client, db_session, monkeypatch, status, held):
    def handler(request):
        return httpx.Response(
            status, json={"error": {"message": "private provider detail"}}, headers={"Retry-After": "7"}
        )

    partner, headers, upstream = await setup(db_session, monkeypatch, handler)
    body = {"model": "gpt-5.4", "input": "Hello", "max_output_tokens": 128}
    result = await client.post("/v1/responses", json=body, headers=headers)
    assert "private provider detail" not in result.text
    await db_session.refresh(partner)
    assert (partner.balance_rub < Decimal("1000000")) is held
    g = (await db_session.execute(select(Generation))).scalar_one()
    assert g.status == ("reconciliation_required" if held else "failed")
    assert (await client.post("/v1/responses", json=body, headers=headers)).status_code == 409
    await upstream.aclose()


async def test_sse_usage_split_chunks_and_messages_auth(client, db_session, monkeypatch):
    events = [
        {
            "type": "message_start",
            "message": {
                "id": "private",
                "usage": {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 30,
                    "output_tokens": 0,
                },
            },
        },
        {"type": "message_delta", "usage": {"output_tokens": 50}},
        {"type": "message_stop"},
    ]
    payload = "".join("event: " + v["type"] + "\ndata: " + json.dumps(v) + "\n\n" for v in events)

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for b in payload.encode():
                yield bytes([b])

    def handler(request):
        return httpx.Response(200, stream=Chunks(), headers={"Content-Type": "text/event-stream"})

    partner, headers, upstream = await setup(db_session, monkeypatch, handler, model="claude-opus-5")
    headers = {"x-api-key": "partner-native-key", "Idempotency-Key": headers["Idempotency-Key"]}
    response = await client.post(
        "/v1/messages",
        headers=headers,
        json={
            "model": "claude-opus-5",
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 128,
            "stream": True,
        },
    )
    assert response.status_code == 200 and "private" not in response.text
    g = (await db_session.execute(select(Generation))).scalar_one()
    assert g.actual_charge_rub == Decimal(".20") and g.status == "completed"
    assert g.usage_snapshot == {
        "input_tokens": 100,
        "cached_input_tokens": 20,
        "cache_write_tokens": 30,
        "output_tokens": 50,
    }
    await upstream.aclose()


async def test_truncated_stream_retains_reserve(client, db_session, monkeypatch):
    def handler(request):
        return httpx.Response(
            200, content=b'data: {"type":"response.created"}\n\n', headers={"Content-Type": "text/event-stream"}
        )

    partner, headers, upstream = await setup(db_session, monkeypatch, handler)
    result = await client.post(
        "/v1/responses",
        headers=headers,
        json={"model": "gpt-5.4", "input": "hi", "stream": True, "max_output_tokens": 128},
    )
    assert result.status_code == 200
    g = (await db_session.execute(select(Generation))).scalar_one()
    assert g.status == "reconciliation_required" and g.actual_charge_rub is None
    await upstream.aclose()


@pytest.mark.parametrize("reference_count", [1, 16])
async def test_image_actual_dimensions_count_and_multipart(client, db_session, monkeypatch, reference_count):
    def encoded(w):
        buf = io.BytesIO()
        Image.new("RGB", (w, 1)).save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()

    def handler(request):
        assert "multipart/form-data; boundary=" in request.headers["Content-Type"]
        assert request.content.count(b'name="image[]"') == reference_count
        assert b'name="mask"' in request.content
        return httpx.Response(200, json={"data": [{"b64_json": encoded(1000)}, {"b64_json": encoded(2048)}]})

    rates = [
        ("default", tier, "generation", Decimal(price), Decimal(".01"))
        for tier, price in [("1K", "10"), ("2K", "20"), ("4K", "30")]
    ]
    partner, headers, upstream = await setup(
        db_session, monkeypatch, handler, model="gpt-image-2", category="image", rates=rates
    )
    r = await client.post(
        "/v1/images/edits",
        headers=headers,
        data={"model": "gpt-image-2", "prompt": "edit", "n": "2"},
        files=[("image[]", (f"x{i}.png", b"input-image", "image/png")) for i in range(reference_count)]
        + [("mask", ("mask.png", b"input-mask", "image/png"))],
    )
    assert r.status_code == 200
    g = (await db_session.execute(select(Generation))).scalar_one()
    assert g.partner_price_rub == Decimal("60") and g.actual_charge_rub == Decimal("30")
    assert g.usage_snapshot == {"1K": 1, "2K": 1}
    assert "input-image" not in json.dumps(g.request_payload)
    assert "b64_json" not in json.dumps(g.request_payload)
    await upstream.aclose()


async def test_native_video_reserves_references_and_preserves_controls(client, db_session, monkeypatch):
    rates = [("default", "480p", "second", Decimal("20"), Decimal(".078"))]
    partner, headers, upstream = await setup(
        db_session, monkeypatch, lambda r: None, model="seedance-2.5", category="video", rates=rates
    )
    body = {
        "model": "seedance-2.5",
        "prompt": "edit this clip",
        "resolution": "480p",
        "omni_reference_task_type": "edit",
        "reference_videos": [{"url": "https://public.example/clip.mp4"}],
        "reference_audios": [{"url": "https://public.example/voice.mp3"}],
    }
    r = await client.post("/v1/videos/generations", headers=headers, json=body)
    assert r.status_code == 202
    g = await db_session.get(Generation, r.json()["request_id"])
    assert g.request_payload["native_body"] == body and g.request_payload["reserved_units"] == {"seconds": 60}
    assert g.partner_price_rub == Decimal("1200")
    r2 = await client.post("/v1/videos/generations", headers=headers, json=body)
    assert r2.json() == r.json()
    from app.inference.accounting import settle_actual

    await settle_actual(db_session, g, {"seconds": 9})
    await settle_actual(db_session, g, {"seconds": 9})
    assert g.actual_charge_rub == Decimal("180")
    entries = list((await db_session.execute(select(LedgerEntry))).scalars())
    assert len(entries) == 2
    await upstream.aclose()


def test_token_usage_rejects_negative_and_inconsistent_cache():
    with pytest.raises(ValueError):
        token_usage("responses", {"input_tokens": 1, "output_tokens": -1})
    with pytest.raises(ValueError):
        token_usage("responses", {"input_tokens": 1, "output_tokens": 1, "input_tokens_details": {"cached_tokens": 2}})


def test_one_hour_cache_is_not_billed_as_five_minute_cache():
    units = token_usage(
        "messages",
        {
            "input_tokens": 2,
            "output_tokens": 5,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 100,
            "cache_creation": {"ephemeral_1h_input_tokens": 40, "ephemeral_5m_input_tokens": 60},
        },
    )
    assert units == {
        "input_tokens": 2,
        "output_tokens": 5,
        "cached_input_tokens": 3,
        "cache_write_tokens": 60,
        "cache_write_1h_tokens": 40,
    }


async def test_attached_video_can_later_settle_missing_usage_without_replaying_job(
    client, db_session, monkeypatch, admin_headers
):
    from app.providers.models import ProviderAttempt

    rates = [("default", "480p", "second", Decimal("20"), Decimal(".078"))]
    _, headers, upstream = await setup(
        db_session, monkeypatch, lambda r: None, model="seedance-2.5", category="video", rates=rates
    )
    response = await client.post(
        "/v1/videos/generations",
        headers=headers,
        json={"model": "seedance-2.5", "prompt": "test", "duration": 4, "resolution": "480p"},
    )
    generation = await db_session.get(Generation, response.json()["request_id"])
    generation.status = "reconciliation_required"
    attempt = ProviderAttempt(generation_id=generation.id, provider="argolink", status="reconciliation_required")
    db_session.add(attempt)
    await db_session.commit()
    url = f"/api/v1/providers/reconciliation/{generation.id}"
    attached = {
        "outcome": "attach_video",
        "provider_task_id": "confirmed-job",
        "reason": "Verified provider task identity",
    }
    assert (await client.post(url, headers=admin_headers, json=attached)).status_code == 200
    generation.status = attempt.status = "reconciliation_required"
    await db_session.commit()
    settled = {"outcome": "completed", "units": {"seconds": 4}, "reason": "Actual billable duration verified"}
    assert (await client.post(url, headers=admin_headers, json=settled)).status_code == 200
    assert (await client.post(url, headers=admin_headers, json=settled)).status_code == 200
    assert (await client.post(url, headers=admin_headers, json=attached)).json()["status"] == "completed"
    assert generation.actual_charge_rub == Decimal("80")
    assert len(generation.request_payload["reconciliation_history"]) == 2
    assert attempt.provider_task_id == "confirmed-job"
    await upstream.aclose()


@pytest.mark.parametrize("disable", [False, True])
async def test_queued_video_cancel_returns_reserves_and_never_submits(
    client, db_session, monkeypatch, admin_headers, disable
):
    rates = [("default", "480p", "second", Decimal("20"), Decimal(".078"))]

    def forbidden(request):
        raise AssertionError("Cancelled work must not reach the provider")

    owner, headers, upstream = await setup(
        db_session, monkeypatch, forbidden, model="seedance-2.5", category="video", rates=rates
    )
    original = owner.balance_rub
    response = await client.post(
        "/v1/videos/generations",
        headers=headers,
        json={"model": "seedance-2.5", "prompt": "test", "duration": 4, "resolution": "480p"},
    )
    generation_id = response.json()["request_id"]
    if disable:
        owner.status = "disabled"
        await db_session.commit()
        result = await client.post(f"/api/v1/generations/{generation_id}/dispatch", headers=admin_headers)
    else:
        url = f"/api/v1/generations/{generation_id}/cancel"
        result = await client.post(url, headers=headers)
        assert (await client.post(url, headers=headers)).status_code == 200
    assert result.status_code == 200 and result.json()["status"] == "cancelled"
    await db_session.refresh(owner)
    assert owner.balance_rub == original
    await upstream.aclose()


async def test_native_model_discovery_is_public_but_hides_drafts(client, db_session):
    db_session.add_all(
        [
            Model(slug="gpt-6-astra", name="Enabled", modality="llm", status="production"),
            Model(slug="gpt-5.6-sol", name="Draft", modality="llm", status="draft"),
        ]
    )
    await db_session.commit()
    result = await client.get("/v1/models")
    assert result.status_code == 200
    assert [m["id"] for m in result.json()["data"]] == ["gpt-6-astra"]
    assert all(m["owned_by"] == "neironych" for m in result.json()["data"])
