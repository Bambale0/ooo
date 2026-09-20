from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.infrastructure.config import get_settings
from app.infrastructure.logging import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    yield


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

    @app.api_route("/api/v0/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def expired_api_version(path: str, request: Request) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_410_GONE,
            content={"detail": "api_version_expired", "current_version": settings.api_prefix},
        )

    return app
