"""Shared, progressively enhanced public UI using the existing Neironych brand."""

import re
from html import escape

from fastapi.responses import HTMLResponse

from app.api.seo import seo_head
from app.infrastructure.config import get_settings


def telegram_bot_url() -> str:
    username = (get_settings().telegram_bot_username or "").lstrip("@")
    return f"https://t.me/{username}" if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username) else ""


def _telegram_icon() -> str:
    return (
        '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
        '<path d="M21.8 3.1 18.7 20c-.2 1.2-.9 1.5-1.9.9l-4.7-3.5-2.3 2.2c-.3.3-.5.5-1 .5l.3-4.8 '
        '8.8-8c.4-.4-.1-.6-.6-.2L6.4 14 1.7 12.5c-1-.3-1-1 .2-1.5L20.2 4c.8-.3 1.6.2 1.6-.9Z"/>'
        "</svg>"
    )


def page(title: str, body: str, lang: str = "ru", *, pricing: bool = False, landing: bool = False,
         seo_path: str | None = None, seo_description: str | None = None,
         seo_headline: str | None = None, partners: bool = False) -> HTMLResponse:
    ru = lang == "ru"
    bot_url = telegram_bot_url()
    bot_action = (
        f'<a class="button primary telegram-button" href="{bot_url}" target="_blank" '
        f'rel="noopener noreferrer">{_telegram_icon()}{"Telegram-бот" if ru else "Telegram bot"}</a>'
        if bot_url else ""
    )
    bot_header = (
        f'<a class="telegram-link" href="{bot_url}" target="_blank" rel="noopener noreferrer" '
        f'aria-label="{"Открыть Telegram-бота" if ru else "Open Telegram bot"}">{_telegram_icon()}'
        f'<span>{"Telegram-бот" if ru else "Telegram bot"}</span></a>'
        if bot_url else ""
    )

    def t(russian, english):
        return russian if ru else english

    price_url = escape(get_settings().public_api_base_url.rstrip("/") + "/price", quote=True)
    cabinet_url = escape(get_settings().public_api_base_url.rstrip("/") + "/cabinet", quote=True)
    docs_url = f"/guide?lang={lang}"
    contents = ""
    introduction = ""
    if '<nav class="contents">' in body:
        introduction, _, rest = body.partition('<nav class="contents">')
        contents, _, body = rest.partition("</nav>")
        contents = contents.replace(" · ", "")
    if partners:
        headline = "Рекомендуйте Нейроныч.<br><span>Получайте вознаграждение.</span>"
        description = "Приглашайте клиентов в Нейроныч API и на разработку. Отслеживайте начисления в личном кабинете."
        action = (
            f'<a class="button primary" href="{cabinet_url}?section=referrals">Открыть партнёрский кабинет →</a>'
            f'{bot_action}'
        )
        side = ""
    elif landing:
        headline = t("Нейросети.<br><span>В вашем продукте.</span>", "AI models.<br><span>In your product.</span>")
        description = t(
            "Создавайте сервисы с генерацией текста, изображений и видео. "
            "Единый API, понятные контракты и расчёты в рублях.",
            "Build products with text, image and video generation. One API, clear contracts and billing in rubles.",
        )
        action = (
            f'{bot_action}'
            f'<a class="button" href="{docs_url}#connect">{t("Документация", "Documentation")} ↗</a>'
            f'<a class="button" href="{cabinet_url}">{t("Личный кабинет", "Partner cabinet")} →</a>'
        )
        side = ""
    elif pricing:
        headline = "Каталог нейросетей API.<br><span>Цены в рублях.</span>"
        description = ("Каталог API нейросетей для видео, изображений и текста. "
                       "Актуальные тарифы в рублях, разрешения, режимы и параметры интеграции.")
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
        headline = t(
            "Документация.<br><span>От идеи до API.</span>",
            "Documentation.<br><span>From idea to API.</span>",
        )
        description = t(
            "Проверенные примеры запросов, параметры, ошибки и получение результатов. "
            "Работайте с моделями через единый API.",
            "Verified request examples, parameters, errors and result retrieval. "
            "Use models through a unified API.",
        )
        action = (
            f'<a class="button primary" href="#connect">{t("Быстрый старт", "Quickstart")} '
            '<span aria-hidden="true">↗</span></a>'
            f'<a class="button" href="#models">{t("Список моделей", "Model list")}</a>'
        )
        side = (
            '<aside class="sidebar docs-aside"><details class="section-menu" open><summary>'
            f"{t('Разделы документации', 'Documentation sections')}</summary>"
            '<label class="docs-nav-label" for="docs-nav-search">'
            f'{t("Найти раздел", "Find a section")}</label>'
            f'<input id="docs-nav-search" type="search" placeholder="{t("Поиск по разделам…", "Search sections…")}" '
            'autocomplete="off" hidden>'
            f'<nav class="contents" aria-label="{t("Разделы документации", "Reference sections")}">{contents}</nav>'
            '</details><div class="sidebar-note"><span class="eyebrow">API REFERENCE</span>'
            f"<p>{t('Примеры соответствуют контрактам API.', 'Examples match API contracts.')}</p>"
            f'<a href="{price_url}">{t("Каталог и цены в рублях", "Catalog and prices in RUB")} '
            '<span aria-hidden="true">↗</span></a></div></aside>'
        )
    language = ""
    language_path = "/" if landing else "/guide"
    if not pricing and not partners:
        language = (
            f'<nav class="language" aria-label="{t("Язык", "Language")}">'
            f'<a data-language href="{language_path}?lang=ru" lang="ru" {"aria-current=page" if ru else ""}>RU</a>'
            f'<a data-language href="{language_path}?lang=en" lang="en" {"aria-current=page" if not ru else ""}>EN</a>'
            '</nav>'
        )
    docs_tools = ""
    if not landing and not pricing and not partners:
        api_base = escape(get_settings().public_api_base_url.rstrip("/"))
        docs_tools = (
            '<section class="docs-start" aria-label="' + t("Быстрый доступ", "Quick links") + '">'
            '<div class="docs-base"><span>API BASE URL</span>'
            f'<code id="public-api-base">{api_base}</code>'
            f'<button class="copy-button" type="button" data-copy-target="public-api-base">'
            f'{t("Копировать", "Copy")}</button></div>'
            '<div class="docs-quicklinks">'
            f'<a href="#connect"><span>01</span><strong>{t("Подключение", "Connect")}</strong>'
            f'<small>{t("Ключ и первый запрос", "API key and first request")}</small></a>'
            f'<a href="#models"><span>02</span><strong>{t("Модели", "Models")}</strong>'
            f'<small>{t("Доступность и Model ID", "Availability and model IDs")}</small></a>'
            f'<a href="#video"><span>03</span><strong>{t("Генерация", "Generation")}</strong>'
            f'<small>{t("Видео и обработка", "Video and results")}</small></a>'
            f'<a href="#errors"><span>04</span><strong>{t("Ошибки и повторы", "Errors and retries")}</strong>'
            f'<small>{t("Надёжная интеграция", "Reliable integration")}</small></a>'
            '</div></section>'
        )
    # The docs host intentionally exposes only the public reference; keep private cabinet links on site and prices.
    cabinet_nav_link = (
        f'<a href="{cabinet_url}">{t("Личный кабинет", "Cabinet")}</a>' if landing or pricing or partners else ""
    )
    partner_nav_link = (
        f'<a href="/partners" {"aria-current=page" if partners else ""}>{t("Партнёрам", "Partners")}</a>'
        if landing or pricing or partners else ""
    )
    path = seo_path or ("/partners" if partners else "/" if landing else "/models" if pricing else "/docs")
    description = seo_description or description
    headline = escape(seo_headline) if seo_headline else headline
    return HTMLResponse(
        f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="theme-color" content="#05070b">'
        f'<meta name="description" content="{escape(description, quote=True)}"><title>{escape(title)}</title>'
        f'{seo_head(title, description, path, lang)}'
        '<link rel="icon" href="/ui/logo-mark.svg" type="image/svg+xml">'
        '<link rel="stylesheet" href="/ui/brand.css?v=3"><script src="/ui/brand.js?v=2" defer></script>'
        f'</head><body class="{"pricing-page" if pricing else "reference-page" if landing or partners else "docs-page"}">'
        f'<a class="skip-link" href="#main">{t("К содержимому", "Skip to content")}</a>'
        '<header class="site-header"><div class="header-inner">'
        f'<a class="brand" href="/" aria-label="{t("Нейроныч API — главная", "Neironych API — home")}">'
        '<img src="/ui/logo-mark.svg" width="44" height="44" alt="">'
        f"<span>{t('Нейроныч', 'Neironych')}<small>API PLATFORM</small></span></a>"
        f'<nav class="primary-nav" aria-label="{t("Основная навигация", "Main navigation")}">'
        f'<a href="/" {"aria-current=page" if landing else ""}>{t("Обзор", "Overview")}</a>'
        f'<a href="{docs_url}" {"aria-current=page" if not pricing and not landing and not partners else ""}>'
        f"{t('Документация', 'Documentation')}</a>"
        f'<a href="{price_url}" {"aria-current=page" if pricing else ""}>{t("Модели и цены", "Models & pricing")}</a>'
        f'{partner_nav_link}{cabinet_nav_link}</nav>'
        f'{bot_header}'
        f'{language}</div></header><main id="main" tabindex="-1" class="container">'
        '<section class="hero" aria-labelledby="page-title"><div class="hero-copy">'
        f'<p class="eyebrow">{("Партнёрская программа" if partners else t("Нейроныч для разработчиков", "Neironych for developers"))}</p>'
        f'<h1 id="page-title">{headline}</h1><p class="hero-description">{description}</p>'
        f'<div class="hero-actions">{action}</div><div class="hero-tags">'
        f"{'<span>API</span><span>Разработка</span><span>Реферальные начисления</span>' if partners else ''}"
        f"{'' if partners else '<span>' + t('Текст', 'Text') + '</span><span>' + t('Изображения', 'Images') + '</span>'}"
        f"{'' if partners else '<span>' + t('Видео', 'Video') + '</span>'}"
        '</div></div><div class="hero-art" aria-hidden="true">'
        '<span class="orbit orbit-one"></span><span class="orbit orbit-two"></span>'
        '<span class="art-code">{ }</span><img src="/ui/neuronych.webp" width="473" height="1000" alt="">'
        f'<span class="art-note">{t("Умно. Просто. По делу.", "Smart. Simple. To the point.")}</span></div></section>'
        f'<div class="{"landing-layout" if landing or partners else "document-layout"}">{side}'
        f'<article class="document" aria-label="{escape(title, quote=True)}">'
        f"{'<div class=reference-intro>' + introduction + '</div>' if introduction else ''}"
        f"{docs_tools}{body}</article></div>"
        '</main><footer class="site-footer container"><span>Нейроныч · API</span>'
        f'{partner_nav_link}<a href="#main">{t("Наверх", "Back to top")} ↑</a></footer>'
        '<p class="toast" role="status" aria-live="polite" aria-atomic="true"></p></body></html>'
    )
