"""Self-contained public inference documentation, without internal API exposure."""

from html import escape
from importlib.resources import files

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.api.dependencies import DbSession
from app.api.partner_reference import render_reference, table
from app.api.public_ui import page
from app.catalog.models import Model, PartnerPrice
from app.contracts.registry import MODELS
from app.infrastructure.config import get_settings

router = APIRouter()


_ASSETS = {
    "brand.css": "text/css",
    "brand.js": "text/javascript",
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
    enabled = table(
        ("Model", "Model ID", "Endpoint"),
        [(model.name, model.slug, MODELS[model.slug]["endpoint"]) for model in model_rows],
    )
    if not model_rows:
        enabled += (
            "<p>Публично включённых моделей пока нет.</p>"
            if ru
            else "<p>No models are currently enabled for public partner use.</p>"
        )
    body = render_reference(base_url, lang).replace("<!-- enabled-models -->", enabled)
    return page(title, body, lang)


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
@router.get("/docs", response_class=HTMLResponse, include_in_schema=False)
async def docs(db: DbSession, lang: str = Query(default="ru", pattern="^(ru|en)$")):
    return await _public_connection_guide(db, lang)


@router.get("/guide", response_class=HTMLResponse, include_in_schema=False)
async def guide(db: DbSession, lang: str = Query(default="ru", pattern="^(ru|en)$")):
    return await _public_connection_guide(db, lang)


@router.get("/prices", response_class=HTMLResponse, include_in_schema=False)
async def prices(db: DbSession):
    rows = (
        await db.execute(
            select(Model, PartnerPrice)
            .join(PartnerPrice, PartnerPrice.model_id == Model.id)
            .where(Model.status == "production")
            .order_by(Model.slug, PartnerPrice.mode, PartnerPrice.resolution)
        )
    ).all()
    units = {"million_tokens": "млн токенов", "second": "секунда", "generation": "изображение"}
    body = '<h2 class="price-heading">Модели и цены</h2>'
    body += "<p>Цены списания с партнёрского баланса. Цена вашей перепродажи определяется вами.</p>"
    if not rows:
        body += (
            '<div class="empty-state"><h3>Приём заказов ещё не открыт.</h3>'
            "<p>Публичных цен пока нет. Изучите документацию, чтобы подготовить интеграцию.</p>"
            '<a class="button" href="/docs">Открыть документацию</a></div>'
        )
        return page("Цены для партнёров", body, pricing=True)
    body += (
        '<div class="price-tools" hidden><label for="price-search">Поиск по модели, режиму или размеру'
        '<input id="price-search" type="search" placeholder="Название модели или 1080p" '
        'autocomplete="off" aria-controls="price-table" aria-describedby="price-count"></label>'
        '<button id="clear-search" type="button" class="button">Сбросить</button></div>'
        f'<p class="price-count" id="price-count" role="status">Конфигураций: {len(rows)}</p>'
        '<div id="price-table" class="table-scroll" tabindex="0" role="region" aria-label="Цены моделей">'
        '<table><thead><tr><th scope="col">Модель</th><th scope="col">Режим / размер</th>'
        '<th scope="col">Цена, ₽</th><th scope="col">Единица</th></tr></thead><tbody>'
    )
    for model, price in rows:
        body += (
            f"<tr><td>{escape(model.name)}</td><td>{escape(price.mode)} / {escape(price.resolution)}</td>"
            f'<td class="price-number">{price.price_rub:.2f}</td>'
            f"<td>{escape(units.get(price.billing_unit, price.billing_unit))}</td></tr>"
        )
    body += (
        '</tbody></table></div><div id="no-results" class="empty-state" hidden>'
        "<h3>Совпадений нет</h3><p>Попробуйте другое название, режим или размер. "
        "Кнопка «Сбросить» вернёт все цены.</p></div>"
    )
    return page("Цены для партнёров", body, pricing=True)
