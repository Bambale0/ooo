"""Shared, progressively enhanced public UI using the existing Neironych brand."""

from html import escape

from fastapi.responses import HTMLResponse

from app.infrastructure.config import get_settings


def page(title: str, body: str, lang: str = "ru", *, pricing: bool = False) -> HTMLResponse:
    ru = lang == "ru"

    def t(russian, english):
        return russian if ru else english

    price_url = escape(get_settings().public_api_base_url.rstrip("/") + "/prices", quote=True)
    docs_url = f"/docs?lang={lang}"
    contents = ""
    introduction = ""
    if '<nav class="contents">' in body:
        introduction, _, rest = body.partition('<nav class="contents">')
        contents, _, body = rest.partition("</nav>")
        contents = contents.replace(" · ", "")
    if pricing:
        headline = "Прозрачно.<br><span>В рублях.</span>"
        description = "Актуальные цены для партнёров. Выберите модель и конфигурацию под свою задачу."
        action = (
            f'<a class="button primary" href="{docs_url}">Перейти к интеграции <span aria-hidden="true">↗</span></a>'
        )
        side = (
            '<aside class="sidebar price-aside"><p class="eyebrow">О расчётах</p><h2>Один баланс.<br>Все модели.</h2>'
            "<p>Стоимость списывается с партнёрского баланса в рублях. Цену перепродажи вы определяете сами.</p>"
            "<p>Цена зависит от режима и размера. Единица расчёта указана в каждой строке.</p>"
            f'<a href="{docs_url}#models">Как выбрать модель <span aria-hidden="true">→</span></a></aside>'
        )
    else:
        headline = t("Ваши идеи.<br><span>Наш API.</span>", "Your ideas.<br><span>Our API.</span>")
        description = t(
            "Текст, изображения и видео — в вашем продукте. Всё для подключения: от первого запроса до результата.",
            "Text, images and video in your product. Everything you need, from your first request to the result.",
        )
        action = (
            f'<a class="button primary" href="#connect">{t("Начать интеграцию", "Start integrating")} '
            '<span aria-hidden="true">↗</span></a>'
            f'<a class="button" href="#models">{t("Выбрать модель", "Explore models")}</a>'
        )
        side = (
            '<aside class="sidebar"><details class="section-menu" open><summary>'
            f"{t('На этой странице', 'On this page')}</summary>"
            f'<nav class="contents" aria-label="{t("Разделы документации", "Reference sections")}">{contents}</nav>'
            '</details><div class="sidebar-note"><span class="eyebrow">API REFERENCE</span>'
            f"<p>{t('От подключения до готового результата.', 'From connection to a finished result.')}</p>"
            f'<a href="{price_url}">{t("Цены в рублях", "Prices in RUB")} <span aria-hidden="true">↗</span></a>'
            "</div></aside>"
        )
    language = ""
    if not pricing:
        language = (
            f'<nav class="language" aria-label="{t("Язык", "Language")}">'
            f'<a data-language href="/docs?lang=ru" lang="ru" {"aria-current=page" if ru else ""}>RU</a>'
            f'<a data-language href="/docs?lang=en" lang="en" {"aria-current=page" if not ru else ""}>EN</a></nav>'
        )
    return HTMLResponse(
        f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="theme-color" content="#05070b">'
        f'<meta name="description" content="{escape(description, quote=True)}"><title>{escape(title)}</title>'
        '<link rel="icon" href="/ui/logo-mark.svg" type="image/svg+xml">'
        '<link rel="stylesheet" href="/ui/brand.css?v=1"><script src="/ui/brand.js?v=1" defer></script>'
        f'</head><body class="{"pricing-page" if pricing else "reference-page"}">'
        f'<a class="skip-link" href="#main">{t("К содержимому", "Skip to content")}</a>'
        '<header class="site-header"><div class="header-inner">'
        f'<a class="brand" href="{docs_url}" aria-label="{t("Нейроныч API — главная", "Neironych API — home")}">'
        '<img src="/ui/logo-mark.svg" width="44" height="44" alt="">'
        f"<span>{t('Нейроныч', 'Neironych')}<small>API PLATFORM</small></span></a>"
        f'<nav class="primary-nav" aria-label="{t("Основная навигация", "Main navigation")}">'
        f'<a href="{docs_url}" {"" if pricing else "aria-current=page"}>{t("Документация", "Documentation")}</a>'
        f'<a href="{price_url}" {"aria-current=page" if pricing else ""}>{t("Цены", "Pricing")}</a></nav>'
        f'{language}</div></header><main id="main" tabindex="-1" class="container">'
        '<section class="hero" aria-labelledby="page-title"><div class="hero-copy">'
        f'<p class="eyebrow">{t("Нейроныч для разработчиков", "Neironych for developers")}</p>'
        f'<h1 id="page-title">{headline}</h1><p class="hero-description">{description}</p>'
        f'<div class="hero-actions">{action}</div><div class="hero-tags">'
        f"<span>{t('Текст', 'Text')}</span><span>{t('Изображения', 'Images')}</span>"
        f'<span>{t("Видео", "Video")}</span></div></div><div class="hero-art" aria-hidden="true">'
        '<span class="orbit orbit-one"></span><span class="orbit orbit-two"></span>'
        '<span class="art-code">{ }</span><img src="/ui/neuronych.webp" width="473" height="1000" alt="">'
        f'<span class="art-note">{t("Умно. Просто. По делу.", "Smart. Simple. To the point.")}</span></div></section>'
        f'<div class="document-layout">{side}<article class="document" aria-label="{escape(title, quote=True)}">'
        f"{'<div class=reference-intro>' + introduction + '</div>' if introduction else ''}{body}</article></div>"
        '</main><footer class="site-footer container"><span>Нейроныч · API</span>'
        f'<a href="#main">{t("Наверх", "Back to top")} ↑</a></footer>'
        '<p class="toast" role="status" aria-live="polite" aria-atomic="true"></p></body></html>'
    )
