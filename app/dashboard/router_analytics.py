"""Dashboard endpoints for Analytics."""

from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, case, func, select

from app.accounts.models import Partner
from app.api.dependencies import DbSession, get_current_partner
from app.dashboard.schemas_extended import (
    AnalyticsResponse,
    DailyStats,
    ModelStats,
)
from app.generations.models import Generation

router_analytics = APIRouter(prefix="/v1/dashboard", tags=["Partner Dashboard - Analytics"])


@router_analytics.get(
    "/analytics",
    response_model=AnalyticsResponse,
    summary="Get usage analytics",
    description="Returns usage analytics including daily statistics and per-model breakdown.",
)
async def get_analytics(
    db: DbSession,
    partner: Partner = Depends(get_current_partner),
    from_date: datetime | None = Query(None, description="Start date for analytics"),
    to_date: datetime | None = Query(None, description="End date for analytics"),
    group_by: str = Query("day", description="Grouping: day, week, month"),
) -> AnalyticsResponse:
    """Get usage analytics with daily and model-level breakdown."""
    # Default to last 30 days if not specified
    now = datetime.utcnow()
    if not to_date:
        to_date = now
    if not from_date:
        from_date = to_date - timedelta(days=30)

    # Ensure from_date <= to_date
    if from_date > to_date:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="from_date must be <= to_date")

    # Query daily stats
    date_trunc_format = {
        "day": "day",
        "week": "week",
        "month": "month",
    }.get(group_by, "day")

    daily_query = (
        select(
            func.date_trunc(date_trunc_format, Generation.created_at).label("period"),
            func.count(Generation.id).label("total_count"),
            func.sum(case((Generation.status == "completed", 1), else_=0)).label("successful_count"),
            func.sum(case((Generation.status.in_(["failed", "cancelled"]), 1), else_=0)).label("failed_count"),
            func.sum(Generation.cost_rub).label("total_cost"),
            func.coalesce(func.sum(Generation.duration_seconds), 0).label("total_seconds"),
        )
        .where(
            and_(
                Generation.partner_id == partner.id,
                Generation.created_at >= from_date,
                Generation.created_at <= to_date,
            )
        )
        .group_by("period")
        .order_by("period")
    )

    daily_result = await db.execute(daily_query)
    daily_rows = daily_result.all()

    daily_stats = [
        DailyStats(
            date=row.period.strftime("%Y-%m-%d"),
            generations_count=row.total_count,
            successful_count=row.successful_count or 0,
            failed_count=row.failed_count or 0,
            total_cost_rub=abs(row.total_cost or Decimal("0.00")),
            total_video_seconds=row.total_seconds or Decimal("0.00"),
        )
        for row in daily_rows
    ]

    # Query model stats
    model_query = (
        select(
            Generation.model,
            func.count(Generation.id).label("count"),
            func.sum(Generation.cost_rub).label("total_cost"),
            func.avg(Generation.cost_rub).label("avg_cost"),
        )
        .where(
            and_(
                Generation.partner_id == partner.id,
                Generation.created_at >= from_date,
                Generation.created_at <= to_date,
            )
        )
        .group_by(Generation.model)
        .order_by(func.sum(Generation.cost_rub).desc())
    )

    model_result = await db.execute(model_query)
    model_rows = model_result.all()

    model_stats = [
        ModelStats(
            model=row.model,
            generations_count=row.count,
            total_cost_rub=abs(row.total_cost or Decimal("0.00")),
            average_cost_rub=abs(row.avg_cost or Decimal("0.00")),
        )
        for row in model_rows
    ]

    # Calculate totals
    total_generations = sum(s.generations_count for s in daily_stats)
    total_spent = sum(s.total_cost_rub for s in daily_stats)

    return AnalyticsResponse(
        period_start=from_date,
        period_end=to_date,
        daily_stats=daily_stats,
        model_stats=model_stats,
        total_generations=total_generations,
        total_spent_rub=total_spent,
    )
