import json
import logging
from decimal import Decimal

import httpx
from sqlalchemy import select
from test_native_inference import setup

from app.accounts.models import ApiKey
from app.billing.models import CoverageLedgerEntry, LedgerEntry
from app.generations.models import Generation
from app.media.models import MediaAsset
from app.providers.models import ProviderAttempt
from app.webhooks.models import WebhookDelivery, WebhookEvent


async def test_partner_and_admin_can_correlate_full_generation_uuid_chain(
    client, db_session, monkeypatch, admin_headers, caplog
):
    upstream_id = "53a2e4c2-3d2f-4ab0-b36c-34ded9ecb803"
    caplog.set_level(logging.INFO, logger="app.inference.diagnostics")

    def handler(request):
        return httpx.Response(524, headers={"X-Request-ID": upstream_id, "CF-Ray": "0123456789abcdef-FRA"})

    partner, headers, upstream = await setup(
        db_session,
        monkeypatch,
        handler,
        model="nano-banana-pro",
        category="image",
        rates=[("default", tier, "generation", Decimal("10"), Decimal(".03")) for tier in ("1K", "2K", "4K")],
    )
    response = await client.post(
        "/v1/images/edits",
        headers=headers,
        json={"model": "nano-banana-pro", "prompt": "trace", "resolution": "1k", "n": 1},
    )
    assert response.status_code == 503
    generation_id = response.json()["request_id"]
    generation = await db_session.get(Generation, generation_id)
    api_key = (await db_session.execute(select(ApiKey).where(ApiKey.partner_id == partner.id))).scalar_one()
    attempt = (
        await db_session.execute(select(ProviderAttempt).where(ProviderAttempt.generation_id == generation_id))
    ).scalar_one()
    ledger_ids = list(
        (await db_session.execute(select(LedgerEntry.id).where(LedgerEntry.generation_id == generation_id))).scalars()
    )
    coverage_ids = list(
        (
            await db_session.execute(
                select(CoverageLedgerEntry.id).where(CoverageLedgerEntry.generation_id == generation_id)
            )
        ).scalars()
    )
    media = MediaAsset(
        generation_id=generation_id,
        partner_id=partner.id,
        kind="image",
        provider="argolink",
        provider_content_url="https://example.invalid/provider",
        public_url="https://example.invalid/public",
    )
    event = WebhookEvent(
        generation_id=generation_id,
        event_type="failed",
        partner_id=partner.id,
        webhook_url="https://example.invalid/hook",
        payload={"generation_id": generation_id},
        status="failed",
    )
    db_session.add_all([media, event])
    await db_session.flush()
    delivery = WebhookDelivery(event_id=event.id, attempt=1, status="failed")
    db_session.add(delivery)
    await db_session.commit()

    partner_trace = await client.get(f"/api/v1/generations/{generation_id}/trace", headers=headers)
    assert partner_trace.status_code == 200
    public = partner_trace.json()
    assert public["trace_id"] == public["request_id"] == public["generation_id"] == generation_id
    assert public["partner_id"] == partner.id
    assert public["model_id"] == generation.model_id
    assert public["api_key_id"] == api_key.id
    assert public["provider_attempts"] == [{"attempt_id": attempt.id, "status": attempt.status}]
    assert public["ledger_entry_ids"] == ledger_ids
    assert public["coverage_ledger_entry_ids"] == coverage_ids
    assert public["media_asset_ids"] == [media.id]
    assert public["webhook_events"] == [{"event_id": event.id, "status": "failed"}]
    assert public["webhook_deliveries"] == [
        {"delivery_id": delivery.id, "event_id": event.id, "status": "failed", "attempt": 1}
    ]
    serialized = json.dumps(public)
    for forbidden in ("credential_id", "provider_task_id", "upstream_request_id", "raw_error", "argolink"):
        assert forbidden not in serialized

    admin_trace = await client.get(f"/api/v1/generations/admin/{generation_id}/trace", headers=admin_headers)
    assert admin_trace.status_code == 200
    internal = admin_trace.json()
    assert internal["generation_id"] == generation_id
    assert internal["provider_attempts"][0]["attempt_id"] == attempt.id
    assert internal["provider_attempts"][0]["credential_id"] == attempt.credential_id
    assert internal["provider_attempts"][0]["upstream_request_id"] == upstream_id

    trace_logs = [record for record in caplog.records if record.name == "app.inference.diagnostics"]
    assert trace_logs
    submit = next(record for record in trace_logs if record.message == "native_inference_submit_started")
    assert submit.trace_id == generation_id
    assert submit.generation_id == generation_id
    assert submit.partner_id == partner.id
    assert submit.api_key_id == api_key.id
    assert submit.attempt_id == attempt.id

    await upstream.aclose()
