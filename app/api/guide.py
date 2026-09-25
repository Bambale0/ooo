"""Minimal public integration documentation.

The unauthenticated documentation surface intentionally exposes only what is
needed to connect enabled models to the Neironych API: the base URL, partner
authentication header, model discovery and model endpoint families.

Pricing, billing, webhooks, retries, internal/admin endpoints and the full
OpenAPI schema are deliberately not part of the public documentation.
"""

from html import escape

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.api.dependencies import DbSession
from app.catalog.models import Model, PartnerPrice
from app.infrastructure.config import get_settings

router = APIRouter()


def page(title: str, body: str, lang: str = "ru") -> HTMLResponse:
    language_nav = (
        '<nav><a href="/docs?lang=ru">Русский</a> · <a href="/docs?lang=en">English</a></nav>'
    )
    return HTMLResponse(
        f'<!doctype html><html lang="{lang}"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title><style>body{{font:17px/1.6 system-ui;max-width:900px;"
        "margin:40px auto;padding:0 20px;color:#182536}pre{overflow:auto;padding:16px;background:#f1f5f9}"
        "table{border-collapse:collapse;width:100%}td,th{padding:10px;border-bottom:1px solid #ddd;text-align:left}"
        "a{color:#1545ab}h1,h2{line-height:1.2}</style>"
        f"{language_nav}<h1>{escape(title)}</h1>{body}</html>"
    )


async def _public_connection_guide(db: DbSession, lang: str) -> HTMLResponse:
    ru = lang == "ru"
    settings = get_settings()
    base_url = settings.public_api_base_url.rstrip("/")
    title = "Подключение моделей" if ru else "Model connection"

    model_rows = (
        await db.execute(
            select(Model)
            .where(Model.status == "production")
            .order_by(Model.slug)
        )
    ).scalars().all()

    endpoint_by_modality = {
        "llm": "/v1/responses · /v1/chat/completions · /v1/messages",
        "image": "/v1/images/generations · /v1/images/edits",
        "video": "/v1/videos/generations",
    }

    if ru:
        body = (
            "<p>Открытая документация содержит только данные, необходимые для подключения моделей.</p>"
            "<h2>Base URL</h2>"
            f"<pre>{escape(base_url)}</pre>"
            "<h2>Авторизация</h2>"
            "<pre>Authorization: Bearer &lt;PARTNER_API_KEY&gt;</pre>"
            "<h2>Получить список доступных моделей</h2>"
            f"<pre>GET {escape(base_url)}/v1/models</pre>"
            "<h2>Подключение моделей</h2>"
        )
    else:
        body = (
            "<p>The public documentation contains only what is required to connect enabled models.</p>"
            "<h2>Base URL</h2>"
            f"<pre>{escape(base_url)}</pre>"
            "<h2>Authentication</h2>"
            "<pre>Authorization: Bearer &lt;PARTNER_API_KEY&gt;</pre>"
            "<h2>List available models</h2>"
            f"<pre>GET {escape(base_url)}/v1/models</pre>"
            "<h2>Model connection</h2>"
        )

    body += "<table><tr><th>Model</th><th>Model ID</th><th>Endpoint</th></tr>"
    for model in model_rows:
        endpoint = endpoint_by_modality.get(model.modality, "/v1/models")
        body += (
            f"<tr><td>{escape(model.name)}</td>"
            f"<td><code>{escape(model.slug)}</code></td>"
            f"<td><code>{escape(endpoint)}</code></td></tr>"
        )
    body += "</table>"

    if not model_rows:
        body += (
            "<p>Публично включённых моделей пока нет.</p>"
            if ru
            else "<p>No models are currently enabled for public partner use.</p>"
        )

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
