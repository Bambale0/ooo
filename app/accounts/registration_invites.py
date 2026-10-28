"""Single-use admission for automatic partner registration."""

import re
import secrets

from fastapi import HTTPException
from sqlalchemy import func, select

from app.accounts.models import RegistrationInvite
from app.infrastructure.config import get_settings
from app.infrastructure.security import hash_secret
from app.providers.models import ProviderCredential

TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32}$")


async def create_registration_invite(db, *, actor: str) -> tuple[RegistrationInvite, str]:
    token = secrets.token_urlsafe(24)
    invite = RegistrationInvite(token_hash=hash_secret(token), created_by=actor)
    db.add(invite)
    await db.flush()
    return invite, token


async def bind_registration_invite(db, *, telegram_id: str, token: str) -> RegistrationInvite:
    if not TOKEN_PATTERN.fullmatch(token):
        raise HTTPException(409, "registration_invite_invalid")
    invite = await db.scalar(
        select(RegistrationInvite)
        .where(RegistrationInvite.token_hash == hash_secret(token))
        .with_for_update()
    )
    if invite is None or invite.status != "active":
        raise HTTPException(409, "registration_invite_invalid")
    if invite.bound_telegram_id not in (None, telegram_id):
        raise HTTPException(409, "registration_invite_invalid")
    invite.bound_telegram_id = telegram_id
    await db.flush()
    return invite


async def has_registration_eligibility(db, *, telegram_id: str) -> bool:
    if not get_settings().registration_invite_required:
        return True
    credential = await db.scalar(
        select(ProviderCredential.id).where(
            ProviderCredential.provider == "argolink",
            ProviderCredential.registration_telegram_id == telegram_id,
            ProviderCredential.is_active.is_(True),
            ProviderCredential.encrypted_api_key.is_not(None),
        )
    )
    if credential is not None:
        return True
    return (
        await db.scalar(
            select(RegistrationInvite.id).where(
                RegistrationInvite.status == "active",
                RegistrationInvite.bound_telegram_id == telegram_id,
            )
        )
    ) is not None


async def consume_registration_invite(db, *, telegram_id: str, application_id: str) -> None:
    if not get_settings().registration_invite_required:
        return
    if await db.scalar(
        select(ProviderCredential.id).where(
            ProviderCredential.provider == "argolink",
            ProviderCredential.registration_telegram_id == telegram_id,
            ProviderCredential.is_active.is_(True),
            ProviderCredential.encrypted_api_key.is_not(None),
        )
    ):
        return
    invite = await db.scalar(
        select(RegistrationInvite)
        .where(
            RegistrationInvite.status == "active",
            RegistrationInvite.bound_telegram_id == telegram_id,
        )
        .with_for_update()
    )
    if invite is None:
        raise HTTPException(403, "registration_invite_required")
    invite.status = "used"
    invite.used_application_id = application_id
    invite.used_at = func.now()
    await db.flush()


async def revoke_registration_invite(db, *, invite_id: str, actor: str) -> None:
    invite = await db.get(RegistrationInvite, invite_id, with_for_update=True)
    if invite is None or invite.status != "active":
        raise HTTPException(404, "registration_invite_not_found")
    invite.status = "revoked"
    invite.revoked_by = actor
    invite.revoked_at = func.now()
    await db.flush()


async def registration_invite_counts(db) -> tuple[int, int, int]:
    rows = await db.execute(
        select(RegistrationInvite.status, func.count()).group_by(RegistrationInvite.status)
    )
    counts = dict(rows.all())
    return int(counts.get("active", 0)), int(counts.get("used", 0)), int(counts.get("revoked", 0))
