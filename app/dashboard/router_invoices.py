"""Dashboard endpoints for Payment Invoices."""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, select

from app.accounts.models import Partner
from app.api.dependencies import DbSession, get_current_partner
from app.dashboard.schemas_extended import (
    InvoiceRead,
    InvoicesResponse,
)
from app.payments.models import PaymentInvoice

router_invoices = APIRouter(prefix="/dashboard", tags=["Partner Dashboard - Invoices"])


@router_invoices.get(
    "/invoices",
    response_model=InvoicesResponse,
    summary="List payment invoices",
    description="Returns paginated payment invoice history with optional filters by status and date range.",
)
async def list_invoices(
    db: DbSession,
    partner: Partner = Depends(get_current_partner),
    status_filter: str | None = Query(None, alias="status", description="Filter by status"),
    from_date: datetime | None = Query(None, description="Filter invoices after this date"),
    to_date: datetime | None = Query(None, description="Filter invoices before this date"),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of entries to return"),
    offset: int = Query(0, ge=0, description="Number of entries to skip"),
) -> InvoicesResponse:
    """Get paginated invoices history with filters."""
    # Build query conditions
    conditions = [PaymentInvoice.partner_id == partner.id]

    if status_filter:
        conditions.append(PaymentInvoice.status == status_filter)
    if from_date:
        conditions.append(PaymentInvoice.created_at >= from_date)
    if to_date:
        conditions.append(PaymentInvoice.created_at <= to_date)

    # Count total
    count_query = select(func.count()).select_from(PaymentInvoice).where(and_(*conditions))
    total = (await db.execute(count_query)).scalar_one()

    # Fetch invoices
    invoices_query = (
        select(PaymentInvoice)
        .where(and_(*conditions))
        .order_by(PaymentInvoice.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(invoices_query)
    invoices = list(result.scalars().all())

    return InvoicesResponse(
        invoices=[
            InvoiceRead(
                id=inv.id,
                partner_id=inv.partner_id,
                status=inv.status,
                amount_rub=inv.requested_rub,
                payment_method=inv.provider,
                payment_url=inv.invoice_url,
                paid_at=inv.paid_at,
                expires_at=inv.expires_at,
                created_at=inv.created_at,
            )
            for inv in invoices
        ],
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + len(invoices)) < total,
    )
