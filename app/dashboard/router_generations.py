"""Dashboard endpoints for Generations history."""

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, select

from app.accounts.models import Partner
from app.api.dependencies import DbSession, get_current_partner
from app.dashboard.schemas_extended import (
    GenerationListItem,
    GenerationsResponse,
)
from app.generations.models import Generation

router_generations = APIRouter(prefix="/dashboard", tags=["Partner Dashboard - Generations"])


@router_generations.get(
    "/generations",
    response_model=GenerationsResponse,
    summary="List generations",
    description="Returns paginated generation history with optional filters by status, model, and date range.",
)
async def list_generations(
    db: DbSession,
    partner: Partner = Depends(get_current_partner),
    status_filter: str | None = Query(None, alias="status", description="Filter by status"),
    model: str | None = Query(None, description="Filter by model"),
    from_date: datetime | None = Query(None, description="Filter generations after this date"),
    to_date: datetime | None = Query(None, description="Filter generations before this date"),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of entries to return"),
    offset: int = Query(0, ge=0, description="Number of entries to skip"),
) -> GenerationsResponse:
    """Get paginated generations history with filters."""
    # Build query conditions
    conditions = [Generation.partner_id == partner.id]

    if status_filter:
        conditions.append(Generation.status == status_filter)
    if model:
        conditions.append(Generation.model == model)
    if from_date:
        conditions.append(Generation.created_at >= from_date)
    if to_date:
        conditions.append(Generation.created_at <= to_date)

    # Count total
    count_query = select(func.count()).select_from(Generation).where(and_(*conditions))
    total = (await db.execute(count_query)).scalar_one()

    # Fetch generations
    generations_query = (
        select(Generation)
        .where(and_(*conditions))
        .order_by(Generation.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(generations_query)
    generations = list(result.scalars().all())

    # Map to response model
    items = [
        GenerationListItem(
            id=gen.id,
            status=gen.status,
            model=gen.model_slug,
            mode=gen.mode,
            resolution=gen.resolution,
            duration_seconds=gen.duration_seconds,
            cost_rub=gen.actual_charge_rub if gen.actual_charge_rub is not None else Decimal("0.00"),
            created_at=gen.created_at,
            completed_at=gen.client_finalized_at,
            error_code=gen.public_error_code if gen.status in ["failed", "cancelled"] else None,
        )
        for gen in generations
    ]

    return GenerationsResponse(
        generations=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + len(items)) < total,
    )
