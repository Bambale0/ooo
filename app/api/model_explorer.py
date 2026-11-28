"""Public model catalog renderer.

Presentation only: model availability and tariff rows come from the database,
not from client-side state, provider metadata, or unreviewed model examples.
"""

from collections import Counter
from decimal import Decimal
from html import escape

from app.catalog.models import Model, PartnerPrice
from app.catalog.video_edit_pricing import EDIT_MODE, EDIT_MODEL, retail_rate
from app.contracts.registry import MODELS


def _escape(value: object) -> str:
    return escape(str(value), quote=True)


def _unit(value: str) -> str:
    return {
        "generation": "за генерацию",
        "second": "за секунду",
        "million_tokens": "за 1 млн токенов",
    }.get(value, value)


def _modality(modality: str) -> tuple[str, str]:
    return {
        "video": ("video", "Видео"),
        "image": ("image", "Изображения"),
        "llm": ("text", "Текст"),
    }.get(modality, ("other", "Другое"))


def render_catalog(rows: list[tuple[Model, PartnerPrice]], fx: Decimal) -> str:
    """Return a progressive catalog: all published prices work with JS off."""
    title = (
        '<div class="catalog-intro">'
        '<span class="eyebrow">MODEL EXPLORER / НЕЙРОНЫЧ API</span>'
        '<h2 class="price-heading">Модели и тарифы</h2>'
        '<p>Выбирайте модель по задаче, сравнивайте режимы и стоимость. '
        'Розничные тарифы указаны в рублях; индивидуальные тарифы '
        'вашего проекта могут отличаться — проверяйте их в личном кабинете.</p>'
        '<div class="catalog-workflow"><span>01 · Выберите модель</span>'
        '<span>02 · Сверьте режим</span><span>03 · Отправьте запрос</span></div>'
        '</div>'
    )
    if not rows:
        return (
            title
            + '<div class="empty-state"><h3>Приём заказов ещё не открыт.</h3>'
            '<p>Публичных цен пока нет. Изучите документацию, чтобы подготовить интеграцию.</p>'
            '<a class="button" href="/docs">Открыть документацию</a></div>'
        )

    special_rule = ""
    if any(model.slug == EDIT_MODEL and rate.mode == EDIT_MODE for model, rate in rows):
        special_rule = (
            '<aside class="catalog-rule" aria-label="Особенности тарификации Seedance 2.5">'
            '<strong>Seedance 2.5 · видеореференсы</strong>'
            '<p>При наличии входного видео (reference_videos) применяется тариф '
            'режима edit — даже если задача отправлена как reference. '
            'Без входного видео действует обычный тариф default. '
            'Оплачиваемые секунды включают длительность результата и всех входных '
            'видеореференсов по итоговому usage. '
            'Цена edit определяется по действующему курсу на момент запроса.</p>'
            '</aside>'
        )
    counts = Counter(_modality(model.modality)[0] for model, _ in rows)
    model_count = len({model.slug for model, _ in rows})
    controls = (
        '<div class="catalog-summary" aria-label="Сводка каталога">'
        f'<span><strong>{model_count}</strong><small>моделей</small></span>'
        f'<span><strong>{len(rows)}</strong><small>конфигураций</small></span>'
        '<span><strong>₽</strong><small>расчёты в рублях</small></span>'
        '</div>'
        '<div class="catalog-filters" hidden role="group" aria-label="Тип модели">'
        f'<button type="button" class="catalog-filter active" data-kind="all" aria-pressed="true">'
        f'Все <span>{len(rows)}</span></button>'
        f'<button type="button" class="catalog-filter" data-kind="text" aria-pressed="false">'
        f'Текст <span>{counts["text"]}</span></button>'
        f'<button type="button" class="catalog-filter" data-kind="image" aria-pressed="false">'
        f'Изображения <span>{counts["image"]}</span></button>'
        f'<button type="button" class="catalog-filter" data-kind="video" aria-pressed="false">'
        f'Видео <span>{counts["video"]}</span></button>'
        '</div>'
        '<div class="price-tools" hidden>'
        '<label for="price-search">Поиск модели, ID, режима или разрешения'
        '<input id="price-search" type="search" placeholder="Например, seedance или 1080p" '
        'autocomplete="off" aria-controls="price-table" aria-describedby="price-count"></label>'
        '<label class="catalog-sort-label" for="price-sort">Сортировка'
        '<select id="price-sort" aria-controls="price-table">'
        '<option value="name">По названию</option>'
        '<option value="price-asc">Сначала дешевле</option>'
        '<option value="price-desc">Сначала дороже</option>'
        '</select></label>'
        '<button id="clear-search" type="button" class="button">Сбросить</button>'
        '</div>'
        f'<p class="price-count" id="price-count" role="status">Конфигураций: {len(rows)}</p>'
    )
    entries = []
    for model, price in rows:
        kind, label = _modality(model.modality)
        contract = MODELS.get(model.slug)
        endpoint = contract.get("endpoint") if contract else None
        section = {"video": "video", "image": "images", "text": "text"}.get(kind, "models")
        rate = retail_rate(model.slug, price, fx)
        mode = (
            "edit · с видеореференсом"
            if model.slug == EDIT_MODEL and price.mode == EDIT_MODE
            else price.mode
        )
        exact_price = f"{rate:.2f}"
        endpoint_html = (
            f'<code class="catalog-endpoint">{_escape(endpoint)}</code>'
            if endpoint else '<span class="muted">Уточняйте в документации</span>'
        )
        model_html = (
            f'<strong class="catalog-model-name">{_escape(model.name)}</strong>'
            f'<code class="catalog-model-id">{_escape(model.slug)}</code>'
        )
        variant_html = f'<strong>{_escape(mode)}</strong><span>{_escape(price.resolution)}</span>'
        money_html = (
            f'<strong class="price-number">{exact_price} ₽</strong>'
            f'<span>{_escape(_unit(price.billing_unit))}</span>'
        )
        link_html = f'<a class="catalog-docs-link" href="/guide?lang=ru#{section}">Документация ↗</a>'
        if contract:
            link_html += (
                f'<a class="catalog-docs-link" href="/docs?model={_escape(model.slug)}#integrations">'
                'Интеграция ↗</a>'
            )
        entries.append(
            f'<tr data-kind="{kind}" data-name="{_escape(model.name.casefold() + " " + model.slug.casefold())}" '
            f'data-price="{exact_price}">'
            f'<td>{model_html}</td>'
            f'<td><span class="catalog-type catalog-type-{kind}">{_escape(label)}</span></td>'
            f'<td class="catalog-variant">{variant_html}</td>'
            f'<td class="catalog-money">{money_html}</td>'
            f'<td class="catalog-api">{endpoint_html}{link_html}</td></tr>'
        )
    table = (
        '<div id="price-table" class="table-scroll catalog-table" tabindex="0" role="region" '
        'aria-label="Доступные модели и розничные цены">'
        '<table><thead><tr>'
        '<th scope="col">Модель / ID</th><th scope="col">Тип</th>'
        '<th scope="col">Режим / разрешение</th><th scope="col">Цена, ₽</th>'
        '<th scope="col">API / справка</th>'
        '</tr></thead><tbody>' + "".join(entries) + '</tbody></table></div>'
        '<div id="no-results" class="empty-state" hidden>'
        '<h3>Совпадений нет</h3>'
        '<p>Смените категорию или поисковый запрос. Сброс вернёт все модели.</p>'
        '</div>'
        '<p class="catalog-footnote">Показаны только публично включённые модели. '
        'Конкретный список доступных вашему аккаунту режимов и персональных тарифов '
        'смотрите в кабинете. '
        'Внешний прайс не является историей фактических списаний.</p>'
    )
    return title + special_rule + controls + table
