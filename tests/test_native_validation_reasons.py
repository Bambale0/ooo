"""Public safe reason codes keep the existing 422 body and financial boundary."""

from decimal import Decimal

import pytest
from sqlalchemy import select
from test_native_inference import setup

from app.billing.models import LedgerEntry
from app.generations.models import Generation
from app.providers.models import ProviderAttempt


@pytest.mark.parametrize(
    "controls,reason",
    [
        (
            {"omni_reference_task_type": "edit", "reference_images": [{"url": "https://example.org/ref.jpg"}]},
            "edit_requires_single_video",
        ),
        ({"duration": "5"}, "invalid_duration"),
        ({"generate_audio": "false"}, "invalid_generate_audio"),
        ({"aspect_ratio": "adaptive"}, "unsupported_aspect_ratio"),
    ],
)
async def test_rejection_reports_safe_reason_without_money_or_provider_work(
    client,
    db_session,
    monkeypatch,
    caplog,
    controls,
    reason,
):
    def forbidden(request):
        raise AssertionError("Invalid input must not reach a provider")

    partner, headers, upstream = await setup(
        db_session,
        monkeypatch,
        forbidden,
        model="seedance-2.5",
        category="video",
        rates=[("default", "480p", "second", Decimal("20"), Decimal(".078"))],
    )
    body = {
        "model": "seedance-2.5",
        "prompt": "PRIVATE_PROMPT_DO_NOT_LOG",
        "resolution": "480p",
        "reference_videos": [{"url": "https://example.org/source.mp4"}],
        **controls,
    }
    # The HTTP dependency rolls back validation failures and expires ORM state.
    partner_id = partner.id
    try:
        for _ in range(2):
            response = await client.post("/v1/videos/generations", headers=headers, json=body)
            assert response.status_code == 422
            assert response.json() == {"detail": "invalid_request_contract"}
            assert response.headers.get("X-Validation-Error") == reason
            event = next(r for r in reversed(caplog.records) if r.message == "native_inference_rejected")
            assert event.validation_reason == reason
            assert event.trace_id == response.headers["X-Request-Id"]
            assert event.partner_id == partner_id
        await db_session.refresh(partner)
        assert partner.balance_rub == Decimal("1000000")
        for model in (Generation, ProviderAttempt, LedgerEntry):
            assert list((await db_session.scalars(select(model))).all()) == []
        assert body["prompt"] not in caplog.text and "example.org" not in caplog.text
    finally:
        await upstream.aclose()


@pytest.mark.parametrize("error_type", [ValueError, TypeError, AttributeError])
async def test_unknown_validation_exception_never_leaks_in_response_or_logs(
    client,
    db_session,
    monkeypatch,
    caplog,
    error_type,
):
    partner, headers, upstream = await setup(
        db_session,
        monkeypatch,
        lambda r: None,
        model="seedance-2.5",
        category="video",
        rates=[("default", "480p", "second", Decimal("20"), Decimal(".078"))],
    )
    marker = "PRIVATE_EXCEPTION_DO_NOT_LOG"

    def broken(*args, **kwargs):
        raise error_type(marker)

    monkeypatch.setattr("app.inference.router.validate_request", broken)
    try:
        response = await client.post(
            "/v1/videos/generations",
            headers=headers,
            json={"model": "seedance-2.5", "prompt": "Animate"},
        )
        assert response.status_code == 422
        assert response.json() == {"detail": "invalid_request_contract"}
        assert "X-Validation-Error" not in response.headers
        assert marker not in response.text and marker not in caplog.text
        event = next(r for r in reversed(caplog.records) if r.message == "native_inference_rejected")
        assert not getattr(event, "validation_reason", None)
    finally:
        await upstream.aclose()


@pytest.mark.parametrize("mode", ["text", "reference", "edit"])
async def test_silent_video_admission_worker_and_settlement_preserve_false(
    client,
    db_session,
    monkeypatch,
    mode,
):
    import json
    from unittest.mock import AsyncMock

    import httpx

    from app.generations.service import dispatch_generation_to_provider, poll_generation_provider
    from app.providers.argolink import ArgoLinkAdapter

    sent = []
    billable_seconds = 5 if mode == "text" else 10

    def handler(request):
        if request.method == "POST":
            sent.append(json.loads(request.content))
            return httpx.Response(202, json={"request_id": "isolated-silent-video"})
        return httpx.Response(
            200,
            json={
                "request_id": "isolated-silent-video",
                "status": "done",
                "model": "seedance-2.5",
                "usage": {
                    "output_seconds": 5,
                    "reference_video_seconds": billable_seconds - 5,
                    "billed_seconds": billable_seconds,
                },
            },
        )

    partner, headers, upstream = await setup(
        db_session,
        monkeypatch,
        handler,
        model="seedance-2.5",
        category="video",
        rates=[("default", "480p", "second", Decimal("20"), Decimal(".078"))],
    )
    monkeypatch.setattr(
        "app.generations.service.get_partner_provider_adapter",
        AsyncMock(return_value=ArgoLinkAdapter(api_key="isolated-upstream", client=upstream)),
    )
    body = {"model": "seedance-2.5", "prompt": "Animate", "resolution": "480p", "generate_audio": False}
    if mode == "edit":
        body["omni_reference_task_type"] = "edit"
    else:
        body["duration"] = 5
    if mode != "text":
        body["reference_videos"] = [{"url": "https://example.org/source.mp4"}]
    if mode == "reference":
        body["reference_images"] = [{"url": "https://example.org/image.jpg"}]
    try:
        response = await client.post("/v1/videos/generations", headers=headers, json=body)
        assert response.status_code == 202
        duplicate = await client.post("/v1/videos/generations", headers=headers, json=body)
        assert duplicate.json() == response.json()
        generation = await db_session.get(Generation, response.json()["request_id"])
        assert generation.request_payload["native_body"] == body
        attempt = await dispatch_generation_to_provider(db_session, generation)
        await dispatch_generation_to_provider(db_session, generation)
        assert sent == [body]
        attempt.next_poll_at = None
        await db_session.commit()
        await poll_generation_provider(db_session, generation)
        assert generation.status == "completed"
        assert generation.actual_charge_rub == Decimal(20) * billable_seconds
        assert partner.balance_rub == Decimal("1000000") - generation.actual_charge_rub
    finally:
        await upstream.aclose()


def test_validation_reason_allowlist_does_not_accept_header_injection():
    from app.contracts.validation_errors import validation_reason

    assert validation_reason(ValueError("invalid_duration")) == "invalid_duration"
    assert validation_reason(ValueError("invalid_duration\r\nX-Injected: unsafe")) is None
    assert validation_reason(TypeError("invalid_duration")) is None
