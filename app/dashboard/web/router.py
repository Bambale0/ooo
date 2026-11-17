"""Web UI router - abbreviated version for Partner Cabinet."""

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, func, select

from app.accounts.models import ApiKey, Partner
from app.api.dependencies import DbSession
from app.billing.models import LedgerEntry
from app.dashboard.web.auth import require_web_auth
from app.generations.models import Generation
from app.payments.models import PaymentInvoice

router = APIRouter(prefix="/cabinet", tags=["Cabinet Web UI"])
templates = Jinja2Templates(directory="app/dashboard/web/templates")


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard_home(
    request: Request,
    db: DbSession,
    partner: Partner = Depends(require_web_auth),
):
    """Dashboard home."""
    # Get stats
    now = datetime.utcnow()
    month_start = datetime(now.year, now.month, 1)
    
    gen_count = (await db.execute(
        select(func.count(Generation.id)).where(
            and_(Generation.partner_id == partner.id, Generation.created_at >= month_start)
        )
    )).scalar_one()
    
    recent_gens = list((await db.execute(
        select(Generation).where(Generation.partner_id == partner.id)
        .order_by(Generation.created_at.desc()).limit(5)
    )).scalars().all())
    
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "partner": partner,
        "total_generations": gen_count, "recent_generations": recent_gens,
    })


@router.get("/generations", response_class=HTMLResponse)
async def generations_page(
    request: Request,
    db: DbSession,
    partner: Partner = Depends(require_web_auth),
    status: str | None = None,
    page: int = 1,
):
    """Generations list."""
    limit, offset = 20, (page - 1) * 20
    conditions = [Generation.partner_id == partner.id]
    if status:
        conditions.append(Generation.status == status)
    
    total = (await db.execute(select(func.count()).select_from(Generation).where(and_(*conditions)))).scalar_one()
    gens = list((await db.execute(
        select(Generation).where(and_(*conditions)).order_by(Generation.created_at.desc()).limit(limit).offset(offset)
    )).scalars().all())
    
    return templates.TemplateResponse("generations.html", {
        "request": request, "partner": partner, "generations": gens,
        "page": page, "total_pages": (total + limit - 1) // limit, "total": total,
    })


@router.get("/api-keys", response_class=HTMLResponse)
async def api_keys_page(
    request: Request,
    db: DbSession,
    partner: Partner = Depends(require_web_auth),
):
    """API keys management."""
    api_keys = list((await db.execute(
        select(ApiKey).where(ApiKey.partner_id == partner.id).order_by(ApiKey.created_at.desc())
    )).scalars().all())
    
    return templates.TemplateResponse("api_keys.html", {
        "request": request, "partner": partner, "api_keys": api_keys,
    })


@router.get("/wallet", response_class=HTMLResponse)
async def wallet_page(
    request: Request,
    db: DbSession,
    partner: Partner = Depends(require_web_auth),
):
    """Wallet & balance."""
    ledger = list((await db.execute(
        select(LedgerEntry).where(LedgerEntry.partner_id == partner.id)
        .order_by(LedgerEntry.created_at.desc()).limit(20)
    )).scalars().all())
    
    return templates.TemplateResponse("wallet.html", {
        "request": request, "partner": partner, "ledger_entries": ledger,
    })


@router.get("/statistics", response_class=HTMLResponse)
async def statistics_page(
    request: Request,
    db: DbSession,
    partner: Partner = Depends(require_web_auth),
):
    """Statistics."""
    from datetime import timedelta
    
    now, period_start = datetime.utcnow(), datetime.utcnow() - timedelta(days=30)
    
    daily_stats = list((await db.execute(
        select(func.date(Generation.created_at).label("date"), func.count(Generation.id).label("count"))
        .where(and_(Generation.partner_id == partner.id, Generation.created_at >= period_start))
        .group_by("date").order_by("date")
    )).all())
    
    return templates.TemplateResponse("statistics.html", {
        "request": request, "partner": partner, "daily_stats": daily_stats,
    })


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    partner: Partner = Depends(require_web_auth),
):
    """Settings."""
    return templates.TemplateResponse("settings.html", {
        "request": request, "partner": partner,
    })
