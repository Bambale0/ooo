"""Partner Dashboard API Router.

Following FastAPI and REST API best practices:
- Resource-oriented URLs
- Proper HTTP methods and status codes
- Clear error messages
- Comprehensive OpenAPI documentation
- Authentication via dependency injection
- Input validation via Pydantic
- Async/await for all DB operations
"""

from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import selectinload

from app.accounts.models import ApiKey, Partner
from app.api.dependencies import DbSession, get_current_partner
from app.billing.models import LedgerEntry
from app.dashboard.schemas import (
    BalanceDetailRead,
    LedgerEntryRead,
    LedgerQueryParams,
    LedgerResponse,
    ProfileRead,
    ProfileUpdate,
    UsageSummary,
)
from app.dashboard.schemas_extended import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyRead,
    ApiKeyWebhookUpdate,
    AnalyticsQueryParams,
    AnalyticsResponse,
    DailyStats,
    GenerationListItem,
    GenerationsQueryParams,
    GenerationsResponse,
    InvoiceRead,
    InvoicesQueryParams,
    InvoicesResponse,
    ModelStats,
)
from app.generations.models import Generation
from app.infrastructure.security import create_api_key, encrypt_secret, hash_secret
from app.payments.models import PaymentInvoice
from app.webhooks.security import validate_public_webhook_url

router = APIRouter(prefix="/v1/dashboard", tags=["Partner Dashboard"])


# ==================== Profile & Settings ====================


@router.get(
    "/profile",
    response_model=ProfileRead,
    summary="Get partner profile",
    description="Returns the authenticated partner's profile information including company name, project name, and account status.",
)
async def get_profile(
    partner: Partner = Depends(get_current_partner),
) -> ProfileRead:
    """Get current partner profile."""
    return ProfileRead(
        partner_id=partner.id,
        company_name=partner.company_name,
        project_name=partner.project_name,
        payment_email=partner.payment_email,
        status=partner.status,
        is_admin=partner.is_admin,
        created_at=partner.created_at,
    )


@router.patch(
    "/profile",
    response_model=ProfileRead,
    summary="Update partner profile",
    description="Update partner profile settings. Currently only payment_email can be updated.",
)
async def update_profile(
    payload: ProfileUpdate,
    db: DbSession,
    partner: Partner = Depends(get_current_partner),
) -> ProfileRead:
    """Update partner profile settings."""
    if partner.status == "deleted":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account_deleted")

    # Update only provided fields
    if payload.payment_email is not None:
        partner.payment_email = payload.payment_email

    await db.flush()
    await db.refresh(partner)

    return ProfileRead(
        partner_id=partner.id,
        company_name=partner.company_name,
        project_name=partner.project_name,
        payment_email=partner.payment_email,
        status=partner.status,
        is_admin=partner.is_admin,
        created_at=partner.created_at,
    )


# ==================== Balance & Billing ====================


@router.get(
    "/balance",
    response_model=BalanceDetailRead,
    summary="Get balance details",
    description="Returns detailed balance information including credit limit and cost coverage.",
)
async def get_balance(
    response: Response,
    partner: Partner = Depends(get_current_partner),
) -> BalanceDetailRead:
    """Get detailed balance information."""
    response.headers["Cache-Control"] = "no-store"

    return BalanceDetailRead(
        partner_id=partner.id,
        balance_rub=partner.balance_rub,
        credit_limit_rub=partner.credit_limit_rub,
        available_balance_rub=partner.available_balance_rub,
        cost_coverage_rub=partner.cost_coverage_rub,
        currency="RUB",
    )


@router.get(
    "/ledger",
    response_model=LedgerResponse,
    summary="Get ledger history",
    description="Returns paginated transaction history with optional filters by operation type and date range.",
)
async def get_ledger(
    db: DbSession,
    partner: Partner = Depends(get_current_partner),
    operation_type: str | None = Query(None, description="Filter by operation type"),
    from_date: datetime | None = Query(None, description="Filter entries after this date"),
    to_date: datetime | None = Query(None, description="Filter entries before this date"),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of entries to return"),
    offset: int = Query(0, ge=0, description="Number of entries to skip"),
) -> LedgerResponse:
    """Get paginated ledger history with filters."""
    # Build query
    conditions = [LedgerEntry.partner_id == partner.id]

    if operation_type:
        conditions.append(LedgerEntry.operation_type == operation_type)
    if from_date:
        conditions.append(LedgerEntry.created_at >= from_date)
    if to_date:
        conditions.append(LedgerEntry.created_at <= to_date)

    # Count total
    count_query = select(func.count()).select_from(LedgerEntry).where(and_(*conditions))
    total = (await db.execute(count_query)).scalar_one()

    # Fetch entries
    entries_query = (
        select(LedgerEntry)
        .where(and_(*conditions))
        .order_by(LedgerEntry.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(entries_query)
    entries = list(result.scalars().all())

    return LedgerResponse(
        entries=[LedgerEntryRead.model_validate(e) for e in entries],
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + len(entries)) < total,
    )


@router.get(
    "/usage-summary",
    response_model=UsageSummary,
    summary="Get usage summary",
    description="Returns usage summary for the current month including generation counts and spending.",
)
async def get_usage_summary(
    db: DbSession,
    partner: Partner = Depends(get_current_partner),
) -> UsageSummary:
    """Get usage summary for current period (month)."""
    # Calculate current month period
    now = datetime.utcnow()
    period_start = datetime(now.year, now.month, 1)
    if now.month == 12:
        period_end = datetime(now.year + 1, 1, 1)
    else:
        period_end = datetime(now.year, now.month + 1, 1)

    # Query generation statistics
    conditions = [
        Generation.partner_id == partner.id,
        Generation.created_at >= period_start,
        Generation.created_at < period_end,
    ]

    # Get counts by status
    stats_query = select(
        func.count(Generation.id).label("total"),
        func.sum(func.cast(Generation.status == "completed", func.Integer)).label("successful"),
        func.sum(func.cast(Generation.status.in_(["failed", "cancelled"]), func.Integer)).label("failed"),
    ).where(and_(*conditions))

    stats_result = (await db.execute(stats_query)).one()

    # Get total spent
    spent_query = select(func.sum(LedgerEntry.amount_rub)).where(
        and_(
            LedgerEntry.partner_id == partner.id,
            LedgerEntry.operation_type == "generation_charge",
            LedgerEntry.created_at >= period_start,
            LedgerEntry.created_at < period_end,
        )
    )
    total_spent = (await db.execute(spent_query)).scalar_one_or_none() or Decimal("0.00")

    # Get total video seconds
    video_seconds_query = select(func.sum(Generation.duration_seconds)).where(
        and_(
            Generation.partner_id == partner.id,
            Generation.status == "completed",
            Generation.created_at >= period_start,
            Generation.created_at < period_end,
            Generation.duration_seconds.isnot(None),
        )
    )
    total_video_seconds = (await db.execute(video_seconds_query)).scalar_one_or_none() or Decimal("0.00")

    return UsageSummary(
        period_start=period_start,
        period_end=period_end,
        total_generations=stats_result.total or 0,
        successful_generations=stats_result.successful or 0,
        failed_generations=stats_result.failed or 0,
        total_spent_rub=abs(total_spent),  # Charges are negative in ledger
        total_video_seconds=total_video_seconds,
    )


# ==================== API Keys Management ====================


@router.get(
    "/api-keys",
    response_model=list[ApiKeyRead],
    summary="List API keys",
    description="Returns all API keys for the authenticated partner. Only shows prefixes, not full keys.",
)
async def list_api_keys(
    db: DbSession,
    partner: Partner = Depends(get_current_partner),
) -> list[ApiKeyRead]:
    """List all partner's API keys."""
    query = (
        select(ApiKey)
        .where(ApiKey.partner_id == partner.id)
        .order_by(ApiKey.created_at.desc())
    )
    result = await db.execute(query)
    keys = result.scalars().all()

    return [
        ApiKeyRead(
            id=key.id,
            name=key.name,
            key_prefix=key.key_prefix,
            is_active=key.is_active,
            webhook_url=key.webhook_url,
            has_webhook_secret=bool(key.webhook_secret_encrypted),
            created_at=key.created_at,
        )
        for key in keys
    ]


@router.post(
    "/api-keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Create API key",
    description="Creates a new API key. The full key is only returned once - save it securely!",
)
async def create_new_api_key(
    payload: ApiKeyCreate,
    db: DbSession,
    partner: Partner = Depends(get_current_partner),
) -> ApiKeyCreated:
    """Create new API key for partner."""
    if partner.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account_not_active")

    # Generate new API key
    api_key, key_hash = create_api_key()
    key_prefix = api_key[:8]

    # Create database record
    new_key = ApiKey(
        partner_id=partner.id,
        name=payload.name,
        key_hash=key_hash,
        key_prefix=key_prefix,
        is_active=True,
    )
    db.add(new_key)
    await db.flush()
    await db.refresh(new_key)

    return ApiKeyCreated(
        id=new_key.id,
        name=new_key.name,
        key_prefix=key_prefix,
        api_key=api_key,
        created_at=new_key.created_at,
    )


@router.patch(
    "/api-keys/{key_id}/webhook",
    response_model=ApiKeyRead,
    summary="Update webhook configuration",
    description="Updates webhook URL and secret for an API key. Set webhook_url to null to disable webhooks.",
)
async def update_api_key_webhook(
    key_id: str,
    payload: ApiKeyWebhookUpdate,
    db: DbSession,
    partner: Partner = Depends(get_current_partner),
) -> ApiKeyRead:
    """Update webhook configuration for API key."""
    # Fetch key
    query = select(ApiKey).where(ApiKey.id == key_id, ApiKey.partner_id == partner.id)
    result = await db.execute(query)
    api_key = result.scalar_one_or_none()

    if not api_key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="api_key_not_found")

    # Validate webhook URL if provided
    if payload.webhook_url:
        validate_public_webhook_url(payload.webhook_url)
        api_key.webhook_url = payload.webhook_url
    else:
        api_key.webhook_url = None

    # Update webhook secret if provided
    if payload.webhook_secret:
        api_key.webhook_secret_encrypted = encrypt_secret(payload.webhook_secret)
    elif payload.webhook_url is None:
        # Clear secret when disabling webhooks
        api_key.webhook_secret_encrypted = None

    await db.flush()
    await db.refresh(api_key)

    return ApiKeyRead(
        id=api_key.id,
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        is_active=api_key.is_active,
        webhook_url=api_key.webhook_url,
        has_webhook_secret=bool(api_key.webhook_secret_encrypted),
        created_at=api_key.created_at,
    )


@router.delete(
    "/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Deactivate API key",
    description="Deactivates an API key. The key can no longer be used for API requests.",
)
async def deactivate_api_key(
    key_id: str,
    db: DbSession,
    partner: Partner = Depends(get_current_partner),
) -> Response:
    """Deactivate API key."""
    # Fetch key
    query = select(ApiKey).where(ApiKey.id == key_id, ApiKey.partner_id == partner.id)
    result = await db.execute(query)
    api_key = result.scalar_one_or_none()

    if not api_key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="api_key_not_found")

    api_key.is_active = False
    await db.flush()

    return Response(status_code=status.HTTP_204_NO_CONTENT)


