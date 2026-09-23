"""Public integration guide and RUB-only retail prices, without procurement data."""

from html import escape

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.api.dependencies import DbSession
from app.catalog.models import Model, PartnerPrice

router = APIRouter()


def page(title: str, body: str, lang="ru") -> HTMLResponse:
    return HTMLResponse(
        f'<!doctype html><html lang="{lang}"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title><style>body{{font:17px/1.6 system-ui;max-width:900px;"
        "margin:40px auto;padding:0 20px;color:#182536}pre{overflow:auto;padding:16px;background:#f1f5f9}"
        "table{border-collapse:collapse;width:100%}td,th{padding:10px;border-bottom:1px solid #ddd;text-align:left}"
        "a{color:#1545ab}h1,h2{line-height:1.2}</style>"
        f'<nav><a href="/guide?lang=ru">Русский</a> · <a href="/guide?lang=en">English</a>'
        f' · <a href="/prices">Цены, ₽</a> · <a href="/docs">OpenAPI</a></nav><h1>{escape(title)}</h1>{body}</html>'
    )


@router.get("/guide", response_class=HTMLResponse, include_in_schema=False)
async def guide(lang: str = Query(default="ru", pattern="^(ru|en)$")):
    ru = lang == "ru"
    title = "API для партнёров" if ru else "Partner API"
    intro = (
        "Подайте заявку в Telegram-кабинете, дождитесь одобрения, создайте API-ключ и пополните баланс. "
        "Все ключи аккаунта используют общий баланс в рублях."
        if ru
        else "Apply in the Telegram cabinet, wait for approval, create an API key and fund your account. "
        "All account keys share one RUB balance."
    )
    auth = (
        "Передавайте ключ в Authorization: Bearer. Для Messages также поддерживается x-api-key. "
        "Каждый платный POST требует уникальный Idempotency-Key (8–160 символов)."
        if ru
        else "Use Authorization: Bearer. Messages also accepts x-api-key. "
        "Every paid POST requires a unique Idempotency-Key (8–160 characters)."
    )
    retries = (
        "Повтор видео возвращает исходный UUID. Повтор синхронного запроса возвращает 409 и UUID исходного запроса. "
        "После таймаута или обрыва потока не создавайте новый ключ повтора: проверьте исходную операцию. "
        "Резерв удерживается до установления результата; автоматической повторной платной отправки нет."
        if ru
        else "Repeating a video request returns its original UUID. Repeating a synchronous request returns 409 and the "
        "original request UUID. After a timeout or interrupted stream, inspect the original operation before retrying "
        "with a new key. The financial hold remains until the outcome is established; "
        "paid submissions are not replayed."
    )
    billing = (
        "До отправки резервируется верхняя оценка стоимости. Для текста задавайте max_output_tokens / "
        "max_completion_tokens / max_tokens: без лимита резерв может быть большим. Итоговое списание считается "
        "по usage, изображениям или оплачиваемым секундам; разница с резервом возвращается. "
        "Параметры и модель не заменяются автоматически."
        if ru
        else "An estimated maximum charge is reserved before submission. "
        "Set max_output_tokens / max_completion_tokens / "
        "max_tokens to avoid a large default hold. Final charges use token usage, images or billable seconds; "
        "unused reserves are released. Parameters and models are never silently substituted."
    )
    example = """curl "$API_BASE/v1/chat/completions" \\
  -H "Authorization: Bearer $PARTNER_API_KEY" \\
  -H "Idempotency-Key: your-unique-request-id" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"MODEL_FROM_V1_MODELS",
       "messages":[{"role":"user","content":"Hello"}],"max_tokens":32}' """
    body = f"<p>{intro}</p><h2>{'Авторизация' if ru else 'Authentication'}</h2><p>{auth}</p>"
    body += f"<pre>{escape(example)}</pre><h2>{'Протоколы' if ru else 'Protocols'}</h2>"
    body += "<pre>GET /v1/models\nPOST /v1/responses\nPOST /v1/chat/completions\nPOST /v1/messages\n"
    body += "POST /v1/images/generations\nPOST /v1/images/edits (JSON / multipart)\nPOST /v1/videos/generations\n"
    body += "GET /v1/videos/{id}\nGET /v1/videos/{id}/content\nPOST /v1/media/uploads</pre>"
    body += f"<h2>{'Расчёты' if ru else 'Billing'}</h2><p>{billing}</p>"
    body += f"<h2>{'Повторы и ошибки' if ru else 'Retries and errors'}</h2><p>{retries}</p>"
    body += (
        "<p>401 — authentication · 402 — balance · 409 — conflict · "
        "422 — request · 429 — rate limit · 503 — unavailable.</p>"
    )
    body += f"<p>{'Текущие модели и цены' if ru else 'Current models and prices'}: "
    body += '<a href="/api/v1/catalog/models">/api/v1/catalog/models</a> · <a href="/prices">/prices</a>.</p>'
    return page(title, body, lang)


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
