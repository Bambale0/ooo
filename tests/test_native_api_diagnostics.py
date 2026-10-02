import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from test_native_inference import setup

from app.generations.models import Generation
from app.providers.models import ProviderAttempt


async def nano_setup(db, monkeypatch, handler):
    return await setup(
        db,
        monkeypatch,
        handler,
        model="nano-banana-pro",
        category="image",
        rates=[("default", tier, "generation", Decimal("10"), Decimal(".03")) for tier in ("1K", "2K", "4K")],
    )


def nano_body():
    return {
        "model": "nano-banana-pro",
        "prompt": 'Keep the "cup".\nText: \\ path {unchanged}.',
        "images": [
            {"image_url": "https://reference.example/one.png"},
            {"image_url": "https://reference.example/two.png"},
        ],
        "aspect_ratio": "4:3",
        "resolution": "2k",
        "n": 1,
        "response_format": "b64_json",
    }


async def test_native_524_preserves_diagnostics_without_replaying_or_exposing_provider(
    client,
    db_session,
    monkeypatch,
    caplog,
):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            524,
            headers={"CF-Ray": "0123456789abcdef-FRA", "Server": "cloudflare"},
            text="private upstream response body",
        )

    partner, headers, upstream = await nano_setup(db_session, monkeypatch, handler)
    body = nano_body()
    response = await client.post("/v1/images/edits", headers=headers, json=body)
    assert response.status_code == 503
    result = response.json()
    assert result["error"]["type"] == "submission_outcome_unknown"
    generation = await db_session.get(Generation, result["request_id"])
    attempt = (
        await db_session.execute(select(ProviderAttempt).where(ProviderAttempt.generation_id == generation.id))
    ).scalar_one()
    assert generation.status == attempt.status == "reconciliation_required"
    assert attempt.provider_task_id is None
    assert json.loads(calls[0].content) == body
    assert calls[0].extensions["timeout"]["read"] == 600
    diagnostics = json.loads(attempt.raw_error)
    assert diagnostics["upstream_status"] == 524
    assert diagnostics["cf_ray"] == "0123456789abcdef-FRA"
    assert diagnostics["generation_id"] == generation.id
    assert diagnostics["attempt_id"] == attempt.id
    assert diagnostics["model"] == "nano-banana-pro"
    assert diagnostics["phase"] == "response_headers"
    assert diagnostics["submit_elapsed_ms"] >= 0
    assert "private upstream response body" not in attempt.raw_error
    assert "reference.example" not in attempt.raw_error
    assert body["prompt"] not in attempt.raw_error
    assert "0123456789abcdef-FRA" not in response.text
    assert "argolink" not in response.text
    duplicate = await client.post("/v1/images/edits", headers=headers, json=body)
    assert duplicate.status_code == 409 and duplicate.json()["request_id"] == generation.id
    assert len(calls) == 1
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("999990")
    assert any(getattr(r, "generation_id", None) == generation.id for r in caplog.records)
    await upstream.aclose()


async def test_nano_pro_success_preserves_model_quotes_newlines_and_reference_order(
    client,
    db_session,
    monkeypatch,
    caplog,
):
    import base64
    import io
    import logging

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (64, 64)).save(buffer, format="JPEG")
    payload = {"data": [{"b64_json": base64.b64encode(buffer.getvalue()).decode()}]}
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=payload)

    partner, headers, upstream = await nano_setup(db_session, monkeypatch, handler)
    body = nano_body()
    caplog.set_level(logging.INFO, logger="app.inference.diagnostics")
    response = await client.post("/v1/images/edits", headers=headers, json=body)
    assert response.status_code == 200 and response.json() == payload
    assert len(calls) == 1 and json.loads(calls[0].content) == body
    assert calls[0].url.path == "/v1/images/edits"
    assert calls[0].headers["Content-Type"] == "application/json"
    generation = await db_session.get(Generation, response.headers["X-Request-Id"])
    attempt = (
        await db_session.execute(select(ProviderAttempt).where(ProviderAttempt.generation_id == generation.id))
    ).scalar_one()
    assert generation.status == attempt.status == "completed"
    assert attempt.raw_error is None
    traces = [r for r in caplog.records if r.name == "app.inference.diagnostics"]
    assert len(traces) == 2
    assert traces[0].image_reference_count == 2
    assert traces[0].prompt_chars == len(body["prompt"])
    assert traces[0].trace_id == traces[1].trace_id == generation.id
    assert traces[1].upstream_status == 200
    assert body["prompt"] not in caplog.text and "reference.example" not in caplog.text
    assert payload["data"][0]["b64_json"] not in caplog.text
    await upstream.aclose()


async def test_native_429_retains_retry_after_and_releases_reserve(client, db_session, monkeypatch):
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "90"}, json={"error": "internal details"})

    partner, headers, upstream = await nano_setup(db_session, monkeypatch, handler)
    response = await client.post("/v1/images/edits", headers=headers, json=nano_body())
    assert response.status_code == 429 and response.headers["Retry-After"] == "90"
    attempt = (await db_session.execute(select(ProviderAttempt))).scalar_one()
    assert attempt.status == "failed" and json.loads(attempt.raw_error)["upstream_status"] == 429
    await db_session.refresh(partner)
    assert partner.balance_rub == Decimal("1000000")
    await upstream.aclose()


async def test_upstream_diagnostic_headers_are_allowlisted_not_copied(client, db_session, monkeypatch, caplog):
    sensitive = "PRIVATE_DONT_LOG_PROVIDER_SECRET"

    def handler(request):
        return httpx.Response(
            524,
            text=sensitive,
            headers={
                "CF-Ray": sensitive,
                "X-Request-ID": sensitive,
                "Set-Cookie": sensitive,
                "Authorization": "Bearer " + sensitive,
                "Server": sensitive,
            },
        )

    partner, headers, upstream = await nano_setup(db_session, monkeypatch, handler)
    response = await client.post("/v1/images/edits", headers=headers, json=nano_body())
    attempt = (await db_session.execute(select(ProviderAttempt))).scalar_one()
    diagnostics = json.loads(attempt.raw_error)
    assert "cf_ray" not in diagnostics and "upstream_request_id" not in diagnostics
    assert sensitive not in attempt.raw_error and sensitive not in caplog.text and sensitive not in response.text
    await upstream.aclose()


async def test_valid_upstream_uuid_is_internal_and_never_changes_public_request_id(
    client,
    db_session,
    monkeypatch,
):
    upstream_id = "53a2e4c2-3d2f-4ab0-b36c-34ded9ecb803"

    def handler(request):
        return httpx.Response(503, headers={"X-Request-ID": upstream_id})

    partner, headers, upstream = await nano_setup(db_session, monkeypatch, handler)
    response = await client.post("/v1/images/edits", headers=headers, json=nano_body())
    attempt = (await db_session.execute(select(ProviderAttempt))).scalar_one()
    diagnostics = json.loads(attempt.raw_error)
    assert diagnostics["upstream_request_id"] == upstream_id
    assert response.json()["request_id"] == attempt.generation_id
    assert upstream_id not in response.text and attempt.provider_task_id is None
    await upstream.aclose()


async def test_body_read_failure_is_distinguished_from_connect_or_header_timeout(
    client,
    db_session,
    monkeypatch,
):
    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"data":['
            raise httpx.ReadError("PRIVATE_DONT_LOG_BODY_ERROR")

    def handler(request):
        return httpx.Response(200, stream=Interrupted(), headers={"CF-Ray": "0123456789abcdef-FRA"})

    partner, headers, upstream = await nano_setup(db_session, monkeypatch, handler)
    response = await client.post("/v1/images/edits", headers=headers, json=nano_body())
    assert response.status_code == 503
    attempt = (await db_session.execute(select(ProviderAttempt))).scalar_one()
    diagnostics = json.loads(attempt.raw_error)
    assert diagnostics["upstream_status"] == 200
    assert diagnostics["phase"] == "response_body_or_usage"
    assert diagnostics["exception_class"] == "ReadError"
    assert "PRIVATE_DONT_LOG_BODY_ERROR" not in attempt.raw_error
    assert attempt.status == "reconciliation_required"
    await upstream.aclose()


@pytest.mark.parametrize(
    "exception, expected_state, expected_balance",
    [
        (httpx.ReadTimeout, "reconciliation_required", Decimal("999990")),
        (httpx.WriteTimeout, "reconciliation_required", Decimal("999990")),
        (httpx.RemoteProtocolError, "reconciliation_required", Decimal("999990")),
        (httpx.ConnectTimeout, "failed", Decimal("1000000")),
        (httpx.PoolTimeout, "failed", Decimal("1000000")),
        (httpx.ConnectError, "failed", Decimal("1000000")),
    ],
)
async def test_transport_failure_category_is_retained_without_secret_or_paid_replay(
    client,
    db_session,
    monkeypatch,
    caplog,
    exception,
    expected_state,
    expected_balance,
):
    calls = []

    def handler(request):
        calls.append(request)
        raise exception("PRIVATE_TRANSPORT_MESSAGE_DO_NOT_LOG", request=request)

    partner, headers, upstream = await nano_setup(db_session, monkeypatch, handler)
    response = await client.post("/v1/images/edits", headers=headers, json=nano_body())
    assert response.status_code == 503
    attempt = (await db_session.execute(select(ProviderAttempt))).scalar_one()
    diagnostics = json.loads(attempt.raw_error)
    assert diagnostics["exception_class"] == exception.__name__
    assert diagnostics["phase"] == "awaiting_headers"
    assert "upstream_status" not in diagnostics
    assert attempt.status == expected_state
    await db_session.refresh(partner)
    assert partner.balance_rub == expected_balance
    assert "PRIVATE_TRANSPORT_MESSAGE_DO_NOT_LOG" not in attempt.raw_error + caplog.text + response.text
    duplicate = await client.post("/v1/images/edits", headers=headers, json=nano_body())
    assert duplicate.status_code == 409 and len(calls) == 1
    await upstream.aclose()



async def test_validation_rejection_logs_partner_key_and_stage(client, db_session, monkeypatch, caplog):
    import logging

    from app.accounts.models import ApiKey

    partner, headers, upstream = await nano_setup(
        db_session,
        monkeypatch,
        lambda request: (_ for _ in ()).throw(AssertionError("invalid request must not reach provider")),
    )
    await db_session.refresh(partner)
    partner_id = partner.id
    key = (
        await db_session.execute(select(ApiKey).where(ApiKey.partner_id == partner_id))
    ).scalar_one()
    key_id = key.id
    caplog.set_level(logging.WARNING, logger="app.inference.diagnostics")
    response = await client.post("/v1/images/edits", headers=headers, json={**nano_body(), "n": 0})
    assert response.status_code == 422
    events = [record for record in caplog.records if record.getMessage() == "native_inference_rejected"]
    assert len(events) == 1
    event = events[0]
    assert event.partner_id == partner_id
    assert event.api_key_id == key_id
    assert event.failure_stage == "request_validation"
    assert event.error_code == "invalid_request_contract"
    assert event.http_status == 422
    assert event.protocol == "images/edits"
    assert "partner-native-key" not in caplog.text
    await upstream.aclose()


async def test_balance_rejection_logs_exact_stage_and_key(client, db_session, monkeypatch, caplog):
    import logging

    from app.accounts.models import ApiKey

    def handler(request):
        raise AssertionError("insufficient balance must not reach provider")

    partner, headers, upstream = await nano_setup(db_session, monkeypatch, handler)
    await db_session.refresh(partner)
    partner_id = partner.id
    key = (
        await db_session.execute(select(ApiKey).where(ApiKey.partner_id == partner_id))
    ).scalar_one()
    key_id = key.id
    partner.balance_rub = Decimal("0")
    await db_session.commit()
    caplog.set_level(logging.WARNING, logger="app.inference.diagnostics")
    response = await client.post("/v1/images/edits", headers=headers, json=nano_body())
    assert response.status_code == 402
    event = next(record for record in caplog.records if record.getMessage() == "native_inference_rejected")
    assert event.partner_id == partner_id
    assert event.api_key_id == key_id
    assert event.failure_stage == "balance"
    assert event.error_code == "insufficient_balance"
    assert event.http_status == 402
    await upstream.aclose()


async def test_provider_rejection_logs_provider_stage_without_body_or_secret(
    client,
    db_session,
    monkeypatch,
    caplog,
):
    import logging

    from app.accounts.models import ApiKey

    sensitive = "PRIVATE_PROVIDER_BODY_MUST_NOT_LOG"

    def handler(request):
        return httpx.Response(400, json={"error": sensitive})

    partner, headers, upstream = await nano_setup(db_session, monkeypatch, handler)
    await db_session.refresh(partner)
    partner_id = partner.id
    key = (
        await db_session.execute(select(ApiKey).where(ApiKey.partner_id == partner_id))
    ).scalar_one()
    key_id = key.id
    caplog.set_level(logging.WARNING, logger="app.inference.diagnostics")
    response = await client.post("/v1/images/edits", headers=headers, json=nano_body())
    assert response.status_code == 422
    events = [record for record in caplog.records if record.getMessage() == "native_inference_rejected"]
    assert len(events) == 1
    event = events[0]
    assert event.partner_id == partner_id
    assert event.api_key_id == key_id
    assert event.failure_stage == "provider_response"
    assert event.error_code == "provider_rejected_request"
    assert event.upstream_status == 400
    assert event.generation_id
    assert sensitive not in caplog.text
    assert "partner-native-key" not in caplog.text
    await upstream.aclose()
