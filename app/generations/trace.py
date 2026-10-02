"""Tenant-safe correlation views for a generation and its durable side effects."""

import json
from uuid import UUID

from sqlalchemy import select

from app.billing.models import CoverageLedgerEntry, LedgerEntry
from app.generations.models import Generation
from app.media.models import MediaAsset
from app.providers.models import ProviderAttempt
from app.webhooks.models import WebhookDelivery, WebhookEvent


def _uuid(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = UUID(value)
    except ValueError:
        return None
    return str(parsed) if str(parsed) == value.lower() else None


def _upstream_request_id(raw_error: str | None) -> str | None:
    if not raw_error:
        return None
    try:
        value = json.loads(raw_error)
    except (TypeError, ValueError):
        return None
    return _uuid(value.get("upstream_request_id")) if isinstance(value, dict) else None


async def generation_trace(db, generation: Generation, *, admin: bool) -> dict[str, object]:
    attempts = list(
        (
            await db.execute(
                select(ProviderAttempt)
                .where(ProviderAttempt.generation_id == generation.id)
                .order_by(ProviderAttempt.created_at, ProviderAttempt.id)
            )
        ).scalars()
    )
    ledger_ids = list(
        (
            await db.execute(
                select(LedgerEntry.id)
                .where(LedgerEntry.generation_id == generation.id)
                .order_by(LedgerEntry.created_at, LedgerEntry.id)
            )
        ).scalars()
    )
    coverage_ids = list(
        (
            await db.execute(
                select(CoverageLedgerEntry.id)
                .where(CoverageLedgerEntry.generation_id == generation.id)
                .order_by(CoverageLedgerEntry.created_at, CoverageLedgerEntry.id)
            )
        ).scalars()
    )
    media_ids = list(
        (
            await db.execute(
                select(MediaAsset.id)
                .where(MediaAsset.generation_id == generation.id)
                .order_by(MediaAsset.created_at, MediaAsset.id)
            )
        ).scalars()
    )
    events = list(
        (
            await db.execute(
                select(WebhookEvent)
                .where(WebhookEvent.generation_id == generation.id)
                .order_by(WebhookEvent.created_at, WebhookEvent.id)
            )
        ).scalars()
    )
    event_ids = [event.id for event in events]
    deliveries = (
        list(
            (
                await db.execute(
                    select(WebhookDelivery)
                    .where(WebhookDelivery.event_id.in_(event_ids))
                    .order_by(WebhookDelivery.created_at, WebhookDelivery.id)
                )
            ).scalars()
        )
        if event_ids
        else []
    )
    snapshot = generation.request_payload or {}
    trace: dict[str, object] = {
        "trace_id": generation.id,
        "request_id": generation.id,
        "generation_id": generation.id,
        "partner_id": generation.partner_id,
        "model_id": generation.model_id,
        "api_key_id": _uuid(snapshot.get("api_key_id")),
        "provider_attempts": [{"attempt_id": item.id, "status": item.status} for item in attempts],
        "ledger_entry_ids": ledger_ids,
        "coverage_ledger_entry_ids": coverage_ids,
        "media_asset_ids": media_ids,
        "webhook_events": [{"event_id": event.id, "status": event.status} for event in events],
        "webhook_deliveries": [
            {
                "delivery_id": delivery.id,
                "event_id": delivery.event_id,
                "status": delivery.status,
                "attempt": delivery.attempt,
            }
            for delivery in deliveries
        ],
    }
    if admin:
        trace["provider_attempts"] = [
            {
                "attempt_id": item.id,
                "status": item.status,
                "provider": item.provider,
                "credential_id": item.credential_id,
                "provider_task_id": item.provider_task_id,
                "upstream_request_id": _upstream_request_id(item.raw_error),
                "public_error_code": item.public_error_code,
            }
            for item in attempts
        ]
    return trace
