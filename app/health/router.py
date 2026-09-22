from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.dependencies import DbSession
from app.infrastructure import state as infrastructure_state
from app.infrastructure.config import get_settings
from app.infrastructure.redis import create_redis_client

router = APIRouter()


@router.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "revision": infrastructure_state.APP_REVISION})


@router.get("/readiness")
async def readiness(db: DbSession) -> JSONResponse:
    checks: dict[str, str] = {}
    ready = True
    try:
        await db.execute(text("select 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "failed"
        ready = False

    redis_client = create_redis_client()
    try:
        checks["redis"] = "ok" if await redis_client.ping() else "failed"
    except Exception:
        checks["redis"] = "degraded"
    finally:
        await redis_client.close()
    if get_settings().app_env == "production" and checks.get("redis") != "ok":
        ready = False

    return JSONResponse(
        status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "status": "ready" if ready else "not_ready",
            "checks": checks,
            "revision": infrastructure_state.APP_REVISION,
        },
    )
