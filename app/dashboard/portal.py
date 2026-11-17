"""Authenticated browser workflows for the partner portal.

This is a BFF: no machine API key is ever stored in the browser. Every query is
tenant-scoped to the server-side Telegram session, and every mutation uses CSRF.
"""

import csv
import hmac
import io
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select

from app.accounts.models import ApiKey, Partner
from app.api.dependencies import DbSession
from app.billing.models import LedgerEntry
from app.catalog.models import Model, PartnerModelGrant, PartnerPrice, PartnerPriceSnapshot
from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.payments.models import PaymentInvoice

router = APIRouter(tags=["Partner portal"], include_in_schema=False)
_PAGE_SIZE = 25
_EXPORT_LIMIT = 1000
_MAX_PERIOD = 90
_ALLOWED_SECTIONS = frozenset(
    {"overview", "activity", "logs", "models", "keys", "credits", "invoices", "ledger", "settings"}
)


def _int_value(value: str | None, *, default: int, minimum: int, maximum: int) -> int:
    if value is None or not value.isascii() or not value.isdecimal():
        return default
    return max(minimum, min(maximum, int(value[:9])))


def _period(request: Request) -> int:
    period = _int_value(request.query_params.get("days"), default=30, minimum=1, maximum=_MAX_PERIOD)
    return period if period in (7, 30, 90) else 30


async def _signed_in(request: Request, db: DbSession) -> tuple[Partner, str]:
    from app.dashboard.web import _partner_from_cookie

    partner, session_id = await _partner_from_cookie(request, db)
    if partner is None or session_id is None:
        raise HTTPException(401, "cabinet_not_authenticated")
    return partner, session_id


def _check_write(request: Request, session_id: str, csrf: str) -> None:
    from app.dashboard.web import _csrf_for_session

    settings = get_settings()
    origin = urlsplit(request.headers.get("origin", ""))
    allowed = urlsplit(settings.public_api_base_url.rstrip("/"))
    if (origin.scheme, origin.netloc) != (allowed.scheme, allowed.netloc):
        raise HTTPException(403, "invalid_cabinet_origin")
    expected = _csrf_for_session(session_id, settings.telegram_bot_token or "")
    if not csrf or not hmac.compare_digest(expected, csrf):
        raise HTTPException(403, "invalid_cabinet_csrf")


async def _page_items(db: DbSession, statement, page: int) -> tuple[list, int]:
    # Counting the filtered subquery avoids SQLAlchemy private internals and
    # preserves every tenant and status predicate of the displayed result set.
    count_query = select(func.count()).select_from(statement.order_by(None).subquery())
    total = (await db.execute(count_query)).scalar_one()
    rows = (await db.execute(statement.limit(_PAGE_SIZE).offset((page - 1) * _PAGE_SIZE))).scalars().all()
    return list(rows), total


async def _usage(db: DbSession, partner_id: str, days: int) -> dict:
    start = datetime.now(UTC) - timedelta(days=days)
    by_day = (
        await db.execute(
            select(
                func.date(Generation.created_at).label("day"),
                func.count(Generation.id).label("requests"),
                func.coalesce(func.sum(Generation.actual_charge_rub), 0).label("spent"),
                func.count(Generation.id).filter(Generation.status == "completed").label("completed"),
                func.count(Generation.id).filter(Generation.status.in_(("failed", "cancelled"))).label("failed"),
            )
            .where(Generation.partner_id == partner_id, Generation.created_at >= start)
            .group_by(func.date(Generation.created_at))
            .order_by(func.date(Generation.created_at))
        )
    ).all()
    by_model = (
        await db.execute(
            select(
                Generation.model_slug,
                func.count(Generation.id).label("requests"),
                func.coalesce(func.sum(Generation.actual_charge_rub), 0).label("spent"),
            )
            .where(Generation.partner_id == partner_id, Generation.created_at >= start)
            .group_by(Generation.model_slug)
            .order_by(func.coalesce(func.sum(Generation.actual_charge_rub), 0).desc())
            .limit(15)
        )
    ).all()
    total_requests = sum(row.requests for row in by_day)
    total_spent = sum((Decimal(row.spent) for row in by_day), Decimal("0"))
    return {
        "days": days,
        "daily": [(str(row.day)[:10], int(row.requests), Decimal(row.spent)) for row in by_day],
        "models": [(row.model_slug, int(row.requests), Decimal(row.spent)) for row in by_model],
        "requests": total_requests,
        "completed": sum(row.completed for row in by_day),
        "failed": sum(row.failed for row in by_day),
        "spent": total_spent,
    }


async def render_portal(request: Request, db: DbSession, partner: Partner, session_id: str) -> HTMLResponse:
    """Render a real, data-backed page, querying only the selected section."""
    from app.dashboard.portal_ui import (
        activity_view,
        credits_view,
        invoices_view,
        keys_view,
        ledger_view,
        logs_view,
        models_view,
        overview_view,
        page_html,
        settings_view,
    )
    from app.dashboard.web import _csrf_for_session

    section = request.query_params.get("section", "overview")
    if section not in _ALLOWED_SECTIONS:
        section = "overview"
    csrf = _csrf_for_session(session_id, get_settings().telegram_bot_token or "")
    current_page = _int_value(request.query_params.get("page"), default=1, minimum=1, maximum=100000)
    notice = request.query_params.get("notice", "")[:40]
    period = _period(request)
    if section == "overview":
        usage = await _usage(db, partner.id, 30)
        recent = (
            await db.execute(
                select(Generation).where(Generation.partner_id == partner.id)
                .order_by(Generation.created_at.desc()).limit(7)
            )
        ).scalars().all()
        active_keys = (
            await db.execute(
                select(func.count()).select_from(ApiKey)
                .where(ApiKey.partner_id == partner.id, ApiKey.is_active.is_(True))
            )
        ).scalar_one()
        body = overview_view(partner, usage, recent, active_keys)
    elif section == "activity":
        usage = await _usage(db, partner.id, period)
        body = activity_view(usage)
    elif section == "logs":
        query = (
            select(Generation).where(Generation.partner_id == partner.id)
            .order_by(Generation.created_at.desc())
        )
        selected_status = request.query_params.get("status", "")
        selected_model = request.query_params.get("model", "")[:80]
        if selected_status in {"queued", "submitting", "processing", "completed", "failed", "cancelled"}:
            query = query.where(Generation.status == selected_status)
        else:
            selected_status = ""
        if selected_model:
            query = query.where(Generation.model_slug == selected_model)
        generations, total = await _page_items(db, query, current_page)
        selected_id = request.query_params.get("generation", "")[:40]
        detail = None
        if selected_id:
            detail = (
                await db.execute(
                    select(Generation).where(Generation.id == selected_id, Generation.partner_id == partner.id)
                )
            ).scalar_one_or_none()
        body = logs_view(generations, total, current_page, selected_status, selected_model, detail)
    elif section == "models":
        query = (
            select(Model, PartnerPrice, PartnerPriceSnapshot)
            .join(PartnerPrice, PartnerPrice.model_id == Model.id)
            .outerjoin(
                PartnerPriceSnapshot,
                and_(
                    PartnerPriceSnapshot.partner_price_id == PartnerPrice.id,
                    PartnerPriceSnapshot.partner_id == partner.id,
                ),
            )
            .outerjoin(
                PartnerModelGrant,
                and_(
                    PartnerModelGrant.model_id == Model.id,
                    PartnerModelGrant.partner_id == partner.id,
                    PartnerModelGrant.revoked_at.is_(None),
                ),
            )
            .where(
                or_(
                    Model.status == "production",
                    and_(Model.status == "restricted", PartnerModelGrant.id.is_not(None)),
                )
            )
            .order_by(Model.name, PartnerPrice.mode, PartnerPrice.resolution)
        )
        tariff_rows = (await db.execute(query)).all()
        search = request.query_params.get("q", "")[:80].strip()
        if search:
            tariff_rows = [
                row for row in tariff_rows
                if search.casefold() in (row[0].slug + " " + row[0].name).casefold()
            ]
        body = await models_view(db, tariff_rows, search)
    elif section == "keys":
        keys = (
            await db.execute(
                select(ApiKey).where(ApiKey.partner_id == partner.id)
                .order_by(ApiKey.created_at.desc()).limit(200)
            )
        ).scalars().all()
        body = keys_view(keys, csrf)
    elif section == "credits":
        latest = (
            await db.execute(
                select(PaymentInvoice).where(PaymentInvoice.partner_id == partner.id)
                .order_by(PaymentInvoice.created_at.desc()).limit(5)
            )
        ).scalars().all()
        body = credits_view(partner, latest, csrf, secrets.token_urlsafe(24))
    elif section == "invoices":
        query = select(PaymentInvoice).where(PaymentInvoice.partner_id == partner.id)
        selected_status = request.query_params.get("status", "")
        if selected_status in {"creating", "active", "paid", "credited", "expired", "refunded", "cancelled"}:
            query = query.where(PaymentInvoice.status == selected_status)
        else:
            selected_status = ""
        query = query.order_by(PaymentInvoice.created_at.desc())
        invoices, total = await _page_items(db, query, current_page)
        body = invoices_view(invoices, total, current_page, selected_status, csrf)
    elif section == "ledger":
        query = select(LedgerEntry).where(LedgerEntry.partner_id == partner.id)
        selected_type = request.query_params.get("type", "")[:64]
        if selected_type:
            query = query.where(LedgerEntry.operation_type == selected_type)
        query = query.order_by(LedgerEntry.created_at.desc())
        entries, total = await _page_items(db, query, current_page)
        body = ledger_view(entries, total, current_page, selected_type)
    else:
        body = settings_view(partner, csrf)
    return page_html(partner, section, body, csrf, notice=notice)


@router.post("/cabinet/action", include_in_schema=False)
async def portal_action(
    request: Request,
    db: DbSession,
    action: str = Form(...),
    csrf: str = Form(...),
    key_id: str = Form(""),
    name: str = Form(""),
    webhook_url: str = Form(""),
    webhook_secret: str = Form(""),
    requested_rub: str = Form(""),
    idempotency_key: str = Form(""),
    buyer_email: str = Form(""),
    payment_id: str = Form(""),
) -> Response:
    from app.dashboard.portal_ui import secret_page
    from app.dashboard.web import _HEADERS

    partner, session_id = await _signed_in(request, db)
    _check_write(request, session_id, csrf)
    if action == "create_key":
        from app.dashboard.router import create_new_api_key
        from app.dashboard.schemas_extended import ApiKeyCreate

        try:
            payload = ApiKeyCreate(name=name.strip())
        except ValidationError as exc:
            raise HTTPException(422, "invalid_api_key_name") from exc
        result = await create_new_api_key(payload, db, partner)
        return secret_page(partner, csrf, result.api_key)
    if action == "revoke_key":
        from app.dashboard.router import deactivate_api_key

        await deactivate_api_key(key_id, db, partner)
        destination = "keys"
    elif action == "set_webhook":
        from app.dashboard.router import update_api_key_webhook
        from app.dashboard.schemas_extended import ApiKeyWebhookUpdate

        try:
            payload = ApiKeyWebhookUpdate(
                webhook_url=webhook_url.strip() or None,
                webhook_secret=webhook_secret or None,
            )
        except ValidationError as exc:
            raise HTTPException(422, "invalid_webhook_configuration") from exc
        await update_api_key_webhook(key_id, payload, db, partner)
        destination = "keys"
    elif action == "update_email":
        from app.payments.schemas import normalize_buyer_email

        if not buyer_email.strip():
            partner.payment_email = None
        else:
            try:
                partner.payment_email = normalize_buyer_email(buyer_email)
            except ValueError as exc:
                raise HTTPException(422, "invalid_payment_email") from exc
        await db.flush()
        destination = "settings"
    elif action == "create_invoice":
        from app.payments.router import create_invoice
        from app.payments.schemas import PaymentInvoiceCreate

        try:
            amount = int(requested_rub)
            payload = PaymentInvoiceCreate(
                requested_rub=amount,
                idempotency_key=idempotency_key,
                buyer_email=buyer_email.strip() or None,
            )
        except (ValueError, ValidationError) as exc:
            raise HTTPException(422, "invalid_invoice_request") from exc
        await create_invoice(payload, db, partner)
        destination = "invoices"
    elif action == "cancel_invoice":
        from app.payments.router import cancel_invoice

        await cancel_invoice(payment_id, db, partner)
        destination = "invoices"
    else:
        raise HTTPException(422, "unknown_cabinet_action")

    return RedirectResponse(
        f"/cabinet?section={destination}&notice=updated",
        status_code=303,
        headers=_HEADERS,
    )


def _csv_safe(value: object) -> str:
    """Neutralize spreadsheet formulas without corrupting numeric values."""
    result = str(value if value is not None else "")
    if result and result[0] in "=+@\t\r\n":
        return "'" + result
    if result.startswith("-") and not result[1:].replace(".", "", 1).isdigit():
        return "'" + result
    return result


@router.get("/cabinet/export/{kind}", include_in_schema=False)
async def portal_export(kind: str, request: Request, db: DbSession) -> Response:
    partner, _ = await _signed_in(request, db)
    writer_buffer = io.StringIO()
    writer = csv.writer(writer_buffer)
    if kind == "logs":
        writer.writerow(("ID", "Date UTC", "Model", "Mode", "Resolution", "Status", "Charged RUB"))
        rows = (
            await db.execute(
                select(Generation).where(Generation.partner_id == partner.id)
                .order_by(Generation.created_at.desc()).limit(_EXPORT_LIMIT)
            )
        ).scalars()
        for item in rows:
            writer.writerow(
                [_csv_safe(x) for x in (
                    item.id, item.created_at, item.model_slug, item.mode, item.resolution,
                    item.status, item.actual_charge_rub,
                )]
            )
    elif kind == "ledger":
        writer.writerow(("Date UTC", "Operation", "Amount RUB", "Balance after RUB"))
        rows = (
            await db.execute(
                select(LedgerEntry).where(LedgerEntry.partner_id == partner.id)
                .order_by(LedgerEntry.created_at.desc()).limit(_EXPORT_LIMIT)
            )
        ).scalars()
        for item in rows:
            writer.writerow(
                [_csv_safe(x) for x in (
                    item.created_at, item.operation_type, item.amount_rub, item.balance_after_rub
                )]
            )
    elif kind == "invoices":
        writer.writerow(("ID", "Date UTC", "Provider", "Status", "Requested RUB", "Credited UTC"))
        rows = (
            await db.execute(
                select(PaymentInvoice).where(PaymentInvoice.partner_id == partner.id)
                .order_by(PaymentInvoice.created_at.desc()).limit(_EXPORT_LIMIT)
            )
        ).scalars()
        for item in rows:
            writer.writerow(
                [_csv_safe(x) for x in (
                    item.id, item.created_at, item.provider, item.status, item.requested_rub, item.credited_at
                )]
            )
    else:
        raise HTTPException(404, "unknown_export")
    return Response(
        content="\ufeff" + writer_buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="neironych-{kind}.csv"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
