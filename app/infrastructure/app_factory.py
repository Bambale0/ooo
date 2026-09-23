from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse

from app.api.dependencies import DbSession
from app.api.router import api_router
from app.infrastructure import state as infrastructure_state
from app.infrastructure.config import get_settings
from app.infrastructure.database import engine
from app.infrastructure.logging import configure_logging
from app.infrastructure.metrics import metrics_payload, monotonic_seconds, observe_http_request, refresh_db_pool_metrics
from app.infrastructure.production_check import require_production_config
from app.payments.crypto_pay import close_crypto_pay_client
from app.providers.http_client import close_provider_http_clients


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    infrastructure_state.APP_REVISION = infrastructure_state._load_revision()
    require_production_config()
    configure_logging()
    try:
        yield
    finally:
        await close_crypto_pay_client()
        await close_provider_http_clients()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )
    app.include_router(api_router, prefix=settings.api_prefix)
    from app.api.guide import router as guide_router

    app.include_router(guide_router)
    from app.inference.router import router as inference_router

    app.include_router(inference_router, prefix="/v1", tags=["Native inference"])
    from app.inference.admin import router as reconciliation_router

    app.include_router(reconciliation_router, prefix=settings.api_prefix + "/providers", tags=["Reconciliation"])

    @app.middleware("http")
    async def prometheus_http_metrics(request: Request, call_next):
        started_at = monotonic_seconds()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            route = _bounded_route_label(app, request)
            if route != "/internal/metrics":
                observe_http_request(
                    method=request.method,
                    route=route,
                    status_code=status_code,
                    duration_seconds=monotonic_seconds() - started_at,
                )

    @app.get("/internal/metrics", include_in_schema=False)
    async def internal_metrics(db: DbSession) -> Response:
        from sqlalchemy import func, select

        from app.generations.models import Generation
        from app.infrastructure.metrics import update_generation_queue_metrics
        from app.infrastructure.retry import utc_now

        rows = (
            await db.execute(
                select(Generation.status, func.count(), func.min(Generation.created_at)).group_by(Generation.status)
            )
        ).all()
        update_generation_queue_metrics({state: (count, oldest) for state, count, oldest in rows}, now=utc_now())
        from app.accounts.models import Partner
        from app.billing.capital import capital_state
        from app.infrastructure.metrics import (
            PARTNER_BALANCE_TOTAL,
            PARTNER_COST_COVERAGE_TOTAL,
            PAYMENTS_PENDING_CREDIT,
            SAFE_TO_WITHDRAW_USDT,
        )
        from app.payments.models import PaymentInvoice

        balance, coverage = (
            await db.execute(
                select(
                    func.coalesce(func.sum(Partner.balance_rub), 0),
                    func.coalesce(func.sum(Partner.cost_coverage_rub), 0),
                )
            )
        ).one()
        PARTNER_BALANCE_TOTAL.set(balance)
        PARTNER_COST_COVERAGE_TOTAL.set(coverage)
        pending = (
            await db.execute(
                select(func.count()).select_from(PaymentInvoice).where(PaymentInvoice.status == "paid_waiting_credit")
            )
        ).scalar()
        PAYMENTS_PENDING_CREDIT.set(pending)
        capital = await capital_state(db)
        SAFE_TO_WITHDRAW_USDT.set(capital["safe"] if capital["safe"] is not None else "NaN")
        refresh_db_pool_metrics(engine.sync_engine.pool)
        payload, content_type = metrics_payload()
        return Response(content=payload, media_type=content_type)

    @app.api_route("/api/v0/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def expired_api_version(path: str, request: Request) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_410_GONE,
            content={"detail": "api_version_expired", "current_version": settings.api_prefix},
        )

    return app


def _bounded_route_label(app: FastAPI, request: Request) -> str:
    from starlette.routing import NoMatchFound

    route = request.scope.get("route")
    if route is None:
        return "/{unmatched}"
    try:
        return str(app.url_path_for(route.name, **{key: "{" + key + "}" for key in request.path_params}))
    except (NoMatchFound, ValueError, TypeError):
        return "/{unmatched}"
