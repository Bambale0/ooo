"""Persistent document-change notices and explicit versioned acceptance."""

from hashlib import sha256

from sqlalchemy import select

from app.accounts.models import ConsentAcceptance, Partner
from app.infrastructure.config import get_settings
from app.telegram.service import notify


async def needs_acceptance(db, partner):
    version = get_settings().legal_document_version
    accepted = set(
        (
            await db.execute(
                select(ConsentAcceptance.document_type).where(
                    ConsentAcceptance.telegram_id == partner.telegram_id,
                    ConsentAcceptance.document_version == version,
                    ConsentAcceptance.accepted.is_(True),
                )
            )
        ).scalars()
    )
    return not {"terms", "privacy_policy"} <= accepted


async def document_notices(db):
    settings = get_settings()
    if not settings.terms_url or not settings.privacy_policy_url:
        return
    version_hash = sha256(settings.legal_document_version.encode()).hexdigest()[:20]
    for partner in (await db.execute(select(Partner).where(Partner.status == "active"))).scalars():
        if await needs_acceptance(db, partner):
            await notify(
                db,
                partner.telegram_id,
                f"Обновлены условия и политика, версия {settings.legal_document_version}. "
                "Откройте /start → Документы для ознакомления и подтверждения.\n"
                f"{settings.terms_url}\n{settings.privacy_policy_url}",
                f"legal:{partner.id}:{version_hash}",
            )


async def accept_current(db, partner):
    if not await needs_acceptance(db, partner):
        return
    for document in ("terms", "privacy_policy"):
        db.add(
            ConsentAcceptance(
                telegram_id=partner.telegram_id,
                partner_id=partner.id,
                document_type=document,
                document_version=get_settings().legal_document_version,
                accepted=True,
            )
        )
