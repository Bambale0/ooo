from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, Response

from app.api.router import api_router
from app.infrastructure.config import get_settings
from app.infrastructure.database import engine
from app.infrastructure.logging import configure_logging
from app.infrastructure.metrics import metrics_payload, monotonic_seconds, observe_http_request, refresh_db_pool_metrics
from app.providers.http_client import close_provider_http_clients


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    try:
        yield
    finally:
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

    @app.middleware("http")
    async def prometheus_http_metrics(request: Request, call_next):
        started_at = monotonic_seconds()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            route_object = request.scope.get("route")
            route = getattr(route_object, "path", "unmatched")
            if route != "/internal/metrics":
                observe_http_request(
                    method=request.method,
                    route=route,
                    status_code=status_code,
                    duration_seconds=monotonic_seconds() - started_at,
                )

    @app.get("/internal/metrics", include_in_schema=False)
    async def internal_metrics() -> Response:
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
