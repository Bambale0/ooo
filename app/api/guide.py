"""Self-contained public inference documentation, without internal API exposure."""

from html import escape

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.api.dependencies import DbSession
from app.api.partner_reference import render_reference, table
from app.catalog.models import Model, PartnerPrice
from app.contracts.registry import MODELS
from app.infrastructure.config import get_settings

router = APIRouter()


def page(title: str, body: str, lang: str = "ru") -> HTMLResponse:
    language_nav = '<nav><a href="/docs?lang=ru">Русский</a> · <a href="/docs?lang=en">English</a></nav>'
    return HTMLResponse(
        f'<!doctype html><html lang="{lang}"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title><style>body{{font:17px/1.6 system-ui;max-width:1100px;"
        "margin:40px auto;padding:0 20px;color:#182536}pre{overflow:auto;padding:16px;background:#f1f5f9}"
        "table{border-collapse:collapse;width:100%}td,th{padding:10px;border-bottom:1px solid #ddd;text-align:left}"
        "a{color:#1545ab}h1,h2{line-height:1.2}h2{margin-top:2.5em;scroll-margin-top:20px}"
        ".table-scroll{overflow-x:auto}td{vertical-align:top;min-width:100px}"
        "summary{cursor:pointer;padding:12px;background:#f1f5f9}.contents{padding:15px;background:#edf3fa}"
        "code{font-size:.9em}pre code{font-size:14px}nav{margin-bottom:24px}</style>"
        f"{language_nav}<h1>{escape(title)}</h1>{body}</html>"
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
    body = "<p>Цены списания с партнёрского баланса. Цена вашей перепродажи определяется вами.</p>"
    body += "<table><tr><th>Модель</th><th>Режим / размер</th><th>Цена, ₽</th><th>Единица</th></tr>"
    for model, price in rows:
        body += (
            f"<tr><td>{escape(model.name)}</td><td>{escape(price.mode)} / {escape(price.resolution)}</td>"
            f"<td>{price.price_rub:.2f}</td><td>{escape(units.get(price.billing_unit, price.billing_unit))}</td></tr>"
        )
    body += "</table>"
    if not rows:
        body += "<p>Приём заказов ещё не открыт.</p>"
    return page("Цены для партнёров", body)
