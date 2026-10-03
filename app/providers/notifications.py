"""Authenticated operational notifications, isolated from generation callbacks."""

import hashlib
import json
import logging
import re
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import DbSession
from app.infrastructure.config import get_settings
from app.telegram.models import BotNotification

logger = logging.getLogger(__name__)
router = APIRouter()
MAX_BODY_BYTES = 64 * 1024


class ProviderNotification(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

    type: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.-]+$")
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=12000)
    values: list[JsonValue] = Field(default_factory=list, max_length=64)
    timestamp: int = Field(ge=0)

    def dedupe_key(self) -> str:
        canonical = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return "provider-notification:argolink:" + hashlib.sha256(canonical.encode()).hexdigest()

    def message(self) -> str:
        values = iter(self.values)

        def replacement(match: re.Match) -> str:
            value = next(values, match.group(0))
            return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

        content = re.sub(r"\{\{value\}\}", replacement, self.content)
        message = f"ArgoLink · {self.type}\n{self.title}\nВремя провайдера: {self.timestamp}\n\n{content}"
        # Keep the durable notification within Telegram's limit, including astral Unicode.
        encoded = message.encode("utf-16-le")
        return message if len(encoded) <= 8000 else encoded[:8000].decode("utf-16-le", errors="ignore") + "\n…"


async def require_notification_secret(authorization: Annotated[str | None, Header()] = None) -> None:
    configured = get_settings().provider_notification_webhook_secret
    if configured is None or not configured.get_secret_value():
        raise HTTPException(503, "provider_notification_not_configured")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(
        token.encode("utf-8"), configured.get_secret_value().encode("utf-8")
    ):
        raise HTTPException(401, "provider_notification_auth_required", headers={"WWW-Authenticate": "Bearer"})


async def enqueue_notification(db: AsyncSession, payload: ProviderNotification, admin_id: str) -> bool:
    key = payload.dedupe_key()
    # The existing unique outbox key arbitrates concurrent deliveries in PostgreSQL.
    # A savepoint keeps a duplicate from invalidating the surrounding transaction.
    try:
        async with db.begin_nested():
            db.add(BotNotification(telegram_id=admin_id, text=payload.message(), dedupe_key=key))
            await db.flush()
    except IntegrityError:
        from sqlalchemy import select

        existing = await db.scalar(select(BotNotification.id).where(BotNotification.dedupe_key == key))
        if existing is None:
            raise
        return False
    return True


@router.post("/webhook/res", include_in_schema=False, dependencies=[Depends(require_notification_secret)])
@router.post("/webhook/res/", include_in_schema=False, dependencies=[Depends(require_notification_secret)])
async def receive_notification(request: Request, db: DbSession) -> dict[str, bool]:
    settings = get_settings()
    admin_id = settings.admin_telegram_id
    if not admin_id or not admin_id.isdigit() or not settings.telegram_bot_token:
        raise HTTPException(503, "provider_notification_delivery_not_configured")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise HTTPException(413, "provider_notification_too_large")
        body.extend(chunk)
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise HTTPException(400, "invalid_json") from None
    try:
        payload = ProviderNotification.model_validate(data)
        created = await enqueue_notification(db, payload, admin_id)
    except (ValidationError, UnicodeError, ValueError):
        raise HTTPException(422, "invalid_provider_notification") from None
    # Acknowledge only committed durable work, even if the Telegram API is down.
    await db.commit()
    logger.info("provider_notification_received", extra={
        "provider": "argolink", "event_type": payload.type, "event_id": payload.dedupe_key(),
        "duplicate": not created,
    })
    return {"success": True}
