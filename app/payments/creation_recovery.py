"""Expire abandoned creation claims without guessing provider acceptance or replaying POST."""

import logging

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.config import get_settings
from app.infrastructure.retry import utc_now
from app.payments.models import PaymentInvoice
from app.telegram.service import notify

logger = logging.getLogger(__name__)


async def expire_lava_creation_claims(
    db: AsyncSession, *, limit: int = 20, payment_id: str | None = None, partner_id: str | None = None,
) -> int:
    now = utc_now()
    query = select(PaymentInvoice).where(
        PaymentInvoice.provider == "lava_top",
        PaymentInvoice.status == "creating",
        PaymentInvoice.provider_reference.is_(None),
        PaymentInvoice.paid_at.is_(None),
        PaymentInvoice.credited_at.is_(None),
        or_(PaymentInvoice.creation_claimed_until.is_(None), PaymentInvoice.creation_claimed_until <= now),
    )
    if payment_id is not None:
        query = query.where(PaymentInvoice.id == payment_id)
    if partner_id is not None:
        query = query.where(PaymentInvoice.partner_id == partner_id)
    rows = (await db.scalars(
        query.order_by(PaymentInvoice.id).limit(limit).with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )).all()
    for payment in rows:
        payment.status = "creation_unknown"
        payment.creation_claimed_until = None
        payment.reconciliation_snapshot = {
            **(payment.reconciliation_snapshot or {}),
            "source": "creation_claim_recovery",
            "result": "creation_lease_expired",
            "observed_at": now.isoformat(),
        }
        await notify(
            db, get_settings().admin_telegram_id,
            f"Создание счёта {payment.id} не подтверждено после истечения срока попытки. "
            "Ссылка не получена; повторный POST автоматически не выполняется. Нужна сверка с Lava.top.",
            f"lava-creation-expired:{payment.id}",
        )
        logger.warning("lava_top_creation_claim_expired", extra={"payment_id": payment.id})
    await db.flush()
    return len(rows)
