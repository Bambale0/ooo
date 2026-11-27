"""Self-contained public inference documentation, without internal API exposure."""

from decimal import Decimal
from importlib.resources import files

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.api.dependencies import DbSession
from app.api.marketing import marketing_page
from app.api.model_explorer import render_catalog
from app.api.native_video_reference import REVIEWED_VIDEO_MODELS, render_native_video_reference
from app.api.partner_reference import render_reference, table
from app.api.public_ui import page
from app.catalog.models import Model, PartnerPrice
from app.catalog.video_edit_pricing import EDIT_MODE, EDIT_MODEL, edit_markup
from app.contracts.registry import MODELS
from app.infrastructure.config import get_settings

router = APIRouter()


_ASSETS = {
    "brand.css": "text/css",
    "brand.js": "text/javascript",
    "portal.css": "text/css",
    "portal.js": "text/javascript",
    "playground.css": "text/css",
    "playground.js": "text/javascript",
    "cabinet.css": "text/css",
    "cabinet.js": "text/javascript",
    "logo-mark.svg": "image/svg+xml",
    "neuronych.webp": "image/webp",
}


@router.get("/ui/{asset}", include_in_schema=False)
async def public_asset(asset: str):
    if asset not in _ASSETS:
        raise HTTPException(status_code=404)
    return Response(
        files("app.api").joinpath("static", asset).read_bytes(),
        media_type=_ASSETS[asset],
        headers={"Cache-Control": "public, max-age=3600", "X-Content-Type-Options": "nosniff"},
    )


async def _public_connection_guide(db: DbSession, lang: str) -> HTMLResponse:
    ru = lang == "ru"
    settings = get_settings()
    base_url = settings.public_api_base_url.rstrip("/")
    title = "Нейроныч API — документация" if ru else "Neironych API reference"

    model_rows = (
        (await db.execute(select(Model).where(Model.status == "production").order_by(Model.slug))).scalars().all()
    )

    model_rows = [model for model in model_rows if model.slug in MODELS]
    enabled = (
        f'<p class="doc-model-count">{len(model_rows)} '
        + ("публично доступных моделей" if ru else "publicly available models")
        + "</p>"
        + table(
            (
                "Модель" if ru else "Model",
                "Model ID",
                "Тип" if ru else "Type",
                "Endpoint",
            ),
            [
                (
                    model.name,
                    model.slug,
                    {"llm": "Текст", "image": "Изображения", "video": "Видео"}.get(
                        model.modality, model.modality
                    ) if ru else model.modality,
                    MODELS[model.slug]["endpoint"],
                )
                for model in model_rows
            ],
        )
    )
    if not model_rows:
        enabled += (
            "<p>Публично включённых моделей пока нет.</p>"
            if ru
            else "<p>No models are currently enabled for public partner use.</p>"
        )
    video_models = {model.slug for model in model_rows}
    body = render_reference(base_url, lang, video_models=video_models - REVIEWED_VIDEO_MODELS).replace(
        "<!-- enabled-models -->", enabled
    )
    anchor = '<h2 id="uploads">'
    body = body.replace(anchor, render_native_video_reference(lang, video_models) + anchor, 1)
    return page(title, body, lang)


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home(lang: str = Query(default="ru", pattern="^(ru|en)$")):
    return marketing_page(lang)


@router.get("/docs", response_class=HTMLResponse, include_in_schema=False)
async def docs(db: DbSession, lang: str = Query(default="ru", pattern="^(ru|en)$")):
    return await _public_connection_guide(db, lang)


@router.get("/guide", response_class=HTMLResponse, include_in_schema=False)
async def guide(db: DbSession, lang: str = Query(default="ru", pattern="^(ru|en)$")):
    return await _public_connection_guide(db, lang)


@router.get("/price", response_class=HTMLResponse, include_in_schema=False)
@router.get("/prices", response_class=HTMLResponse, include_in_schema=False)
@router.get("/models", response_class=HTMLResponse, include_in_schema=False)
async def prices(db: DbSession):
    """Public model explorer; must never expose private model grants or supplier cost."""
    rows = (
        await db.execute(
            select(Model, PartnerPrice)
            .join(PartnerPrice, PartnerPrice.model_id == Model.id)
            .where(Model.status == "production")
            .order_by(Model.slug, PartnerPrice.mode, PartnerPrice.resolution)
        )
    ).all()
    rows = [
        (model, price)
        for model, price in rows
        if not (model.slug == EDIT_MODEL and price.mode == EDIT_MODE and edit_markup(model.slug) is None)
    ]
    fx = Decimal(1)
    if any(model.slug == EDIT_MODEL and price.mode == EDIT_MODE for model, price in rows):
        from app.billing.fx import current_fx

        fx = (await current_fx(db))["rate"]
    return page("Модели и тарифы — Нейроныч API", render_catalog(rows, fx), pricing=True)
