from fastapi import APIRouter

from app.accounts.router import router as accounts_router
from app.billing.router import router as billing_router
from app.catalog.router import router as catalog_router
from app.generations.router import router as generations_router
from app.health.router import router as health_router
from app.media.router import router as media_router
from app.providers.router import router as providers_router

api_router = APIRouter()
api_router.include_router(health_router, tags=["health"])
api_router.include_router(accounts_router, prefix="/accounts", tags=["accounts"])
api_router.include_router(catalog_router, prefix="/catalog", tags=["catalog"])
api_router.include_router(billing_router, prefix="/billing", tags=["billing"])
api_router.include_router(generations_router, prefix="/generations", tags=["generations"])
api_router.include_router(media_router, prefix="/media", tags=["media"])
api_router.include_router(providers_router, prefix="/providers", tags=["providers"])
