"""Self-contained public inference documentation, without internal API exposure."""

from decimal import Decimal
from html import escape
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
from app.api.seo import canonical_url, model_path, sitemap_xml
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
    title = ("Документация API нейросетей — интеграция и примеры | Нейроныч"
             if ru else "AI API documentation and examples | Neironych")

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
    rows = await _priced_rows(db)
    return page("Каталог нейросетей API — модели и цены в рублях | Нейроныч",
                render_catalog(rows, await _catalog_fx(db, rows)), pricing=True)


async def _priced_rows(db: DbSession):
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
    return rows


async def _catalog_fx(db: DbSession, rows):
    fx = Decimal(1)
    if any(model.slug == EDIT_MODEL and price.mode == EDIT_MODE for model, price in rows):
        from app.billing.fx import current_fx

        fx = (await current_fx(db))["rate"]
    return fx


_CATEGORIES = {
    "video": ("video", "Нейросети для генерации видео"),
    "image": ("image", "Нейросети для генерации изображений"),
    "text": ("llm", "Языковые модели и LLM"),
}


@router.get("/models/{slug}", response_class=HTMLResponse, include_in_schema=False)
async def model_detail(slug: str, db: DbSession):
    rows = [(model, price) for model, price in await _priced_rows(db) if model.slug in MODELS]
    if slug in _CATEGORIES:
        modality, heading = _CATEGORIES[slug]
        rows = [(model, price) for model, price in rows if model.modality == modality]
        if not rows:
            raise HTTPException(status_code=404)
        description = f"{heading} через единый API Нейроныч. Сравните доступные модели, режимы и тарифы в рублях."
    else:
        rows = [(model, price) for model, price in rows if model.slug == slug]
        if not rows:
            raise HTTPException(status_code=404)
        model = rows[0][0]
        name = "Seedance 2.0" if model.slug == "seedance-2.0" else model.name
        capability = {"video": "генерация видео", "image": "генерация изображений",
                      "llm": "работа с текстом"}.get(model.modality, "генерация контента")
        heading = f"{name} API — {capability}"
        description = (f"{name} через API Нейроныч: {capability}, актуальные тарифы в рублях, "
                       "поддерживаемые конфигурации и документация для разработчиков.")
    content = '<p><a href="/models">Все модели API</a> · <a href="/docs#models">Документация API</a></p>'
    if slug not in _CATEGORIES:
        endpoint = escape(MODELS[slug]["endpoint"])
        content += (f'<section><h2>Интеграция {escape(name)}</h2>'
                    f'<p>Model ID: <code>{escape(slug)}</code></p><p>Endpoint: <code>{endpoint}</code></p>'
                    '<p>Получите API-ключ в личном кабинете и используйте параметры и примеры из '
                    '<a href="/docs">документации</a>.</p></section>')
    content += render_catalog(rows, await _catalog_fx(db, rows))
    return page(f"{heading} — цены в рублях | Нейроныч", content, pricing=True,
                seo_path=model_path(slug), seo_headline=heading, seo_description=description)


@router.get("/robots.txt", include_in_schema=False)
async def robots():
    # Private paths remain crawlable to let search engines observe X-Robots-Tag: noindex.
    return Response(f"User-agent: *\nAllow: /\nSitemap: {canonical_url('/sitemap.xml')}\n", media_type="text/plain")


@router.get("/sitemap.xml", include_in_schema=False)
async def sitemap(db: DbSession):
    models = {model.slug: model.modality for model, _ in await _priced_rows(db) if model.slug in MODELS}
    paths = ["/", "/?lang=en", "/docs", "/docs?lang=en", "/models"]
    paths.extend(model_path(slug) for slug, (modality, _) in _CATEGORIES.items() if modality in models.values())
    paths.extend(model_path(slug) for slug in sorted(models))
    return Response(sitemap_xml(paths), media_type="application/xml")
