"""Lifetime video trials; accepted submissions are never retried as new jobs."""

import hashlib
import hmac
import time

from fastapi import HTTPException
from sqlalchemy import text

from app.infrastructure.config import get_settings
from app.telegram.models import TrialEntitlement


async def claim_trial(db, telegram_id):
    if db.bind.dialect.name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": "trial:" + telegram_id}
        )
    row = await db.get(TrialEntitlement, telegram_id, with_for_update=True)
    if row is None:
        row = TrialEntitlement(telegram_id=telegram_id, used=0)
        db.add(row)
    if row.used >= 2:
        raise HTTPException(409, "trial_limit_reached")
    row.used += 1
    await db.flush()


def signature(generation_id, partner_id, expires):
    key = get_settings().provider_credentials_master_key
    if not key:
        raise HTTPException(503, "content_not_available")
    return hmac.new(key.encode(), f"trial:{generation_id}:{partner_id}:{expires}".encode(), hashlib.sha256).hexdigest()


def download_link(generation):
    expires = int(time.time()) + 86400
    token = signature(generation.id, generation.partner_id, expires)
    settings = get_settings()
    return (
        f"{settings.public_api_base_url.rstrip('/')}{settings.api_prefix}/media/trials/"
        f"{generation.id}/{expires}/{token}"
    )
