"""Durable treasury alarms; a manual mute belongs to one incident episode."""

from datetime import UTC, timedelta
from uuid import uuid4

from sqlalchemy import select, text

from app.billing.capital import capital_state
from app.billing.fx import current_fx
from app.billing.models import FinancialIncident
from app.catalog.models import Model, PartnerPrice
from app.infrastructure.config import get_settings
from app.infrastructure.retry import utc_now
from app.telegram.service import notify


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


async def observe_incident(db, *, kind: str, negative: bool, detail: str, repeating: bool) -> None:
    row = await db.get(FinancialIncident, kind, with_for_update=True)
    if not row and not negative:
        return
    if row is None:
        row = FinancialIncident(kind=kind, episode=str(uuid4()), recovered=False, muted=False, detail=detail)
        db.add(row)
    elif negative and row.recovered:
        row.episode, row.muted, row.last_alert_at = str(uuid4()), False, None
    row.recovered, row.detail = not negative, detail
    # For treasury, a recovery does not acknowledge the already opened alarm.
    due = row.last_alert_at is None or aware(row.last_alert_at) <= utc_now() - timedelta(minutes=15)
    if not row.muted and due and (negative or repeating):
        now = utc_now()
        await notify(
            db, get_settings().admin_telegram_id, detail, f"incident:{row.episode}:{int(now.timestamp()) // 900}"
        )
        row.last_alert_at = now
        if not repeating:
            row.muted = True
    await db.flush()


async def financial_tick(db) -> None:
    if db.bind.dialect.name == "postgresql":
        await db.execute(text("SELECT pg_advisory_xact_lock(728346020)"))
    state = await capital_state(db)
    safe = state["safe"]
    if safe is not None:
        message = (
            f"Дефицит покрытия: {-safe:.2f} USDT."
            if safe < 0
            else f"Покрытие восстановлено. Доступно к выводу: {safe:.2f} USDT. Подтвердите завершение инцидента."
        )
        message += (
            f"\nДанные кошелька: {state['freshness']}. Остановить уведомления: Казначейство → Заглушить инцидент."
        )
        await observe_incident(db, kind="treasury", negative=safe < 0, detail=message, repeating=True)
    fx = (await current_fx(db))["rate"]
    rows = (
        await db.execute(
            select(Model, PartnerPrice)
            .join(PartnerPrice, Model.id == PartnerPrice.model_id)
            .where(Model.status == "production")
        )
    ).all()
    for model, price in rows:
        negative = price.price_rub < price.provider_cost_usdt * fx
        await observe_incident(
            db,
            kind=f"economics:{price.id}",
            negative=negative,
            repeating=False,
            detail=f"Конфигурация {model.slug} / {price.mode} / {price.resolution} убыточна. "
            "Новые запросы блокируются до исправления цен или курса.",
        )


async def mute_treasury(db, episode: str) -> None:
    from fastapi import HTTPException

    row = await db.get(FinancialIncident, "treasury", with_for_update=True)
    if not row or row.episode != episode:
        raise HTTPException(409, "incident_changed")
    row.muted = True
    await db.flush()
