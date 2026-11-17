"""Server-rendered, accessible OpenRouter-style partner control panel.

No templates, API bearer tokens, inline executable scripts or untrusted HTML.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html import escape
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import HTMLResponse

from app.accounts.models import ApiKey, Partner
from app.billing.models import LedgerEntry
from app.catalog.video_edit_pricing import EDIT_MODE, EDIT_MODEL, edit_markup, retail_rate
from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.payments.models import PaymentInvoice

_SECTIONS = (
    ("overview", "Обзор", "▦", "Рабочее пространство"),
    ("activity", "Аналитика", "◫", "Использование"),
    ("logs", "Запросы", "≡", "Использование"),
    ("models", "Модели и тарифы", "◇", "Использование"),
    ("keys", "API-ключи", "⌘", "Управление"),
    ("credits", "Баланс и пополнение", "◉", "Биллинг"),
    ("invoices", "Счета", "▤", "Биллинг"),
    ("ledger", "История операций", "⇄", "Биллинг"),
    ("settings", "Настройки", "⚙", "Аккаунт"),
)
_TITLES = {id_: label for id_, label, _, _ in _SECTIONS}
_STATUS = {
    "active": "Ожидает оплаты",
    "creating": "Создаётся",
    "paid": "Оплачен",
    "paid_waiting_credit": "Ожидает зачисления",
    "credited": "Зачислен",
    "completed": "Завершено",
    "processing": "Выполняется",
    "submitting": "Отправляется",
    "queued": "В очереди",
    "failed": "Ошибка",
    "cancelled": "Отменено",
    "expired": "Истёк",
    "refunded": "Возвращён",
    "partially_refunded": "Частичный возврат",
    "creation_unknown": "Уточняется",
}
_SUCCESS = {"completed", "credited", "paid"}
_ERROR = {"failed", "cancelled", "expired", "refunded"}


def _e(value: object) -> str:
    return escape(str("—" if value is None else value), quote=True)


def _rub(value: Decimal | None) -> str:
    return f"{Decimal(value or 0):,.2f}".replace(",", " ") + " ₽"


def _dt(value: object) -> str:
    if not isinstance(value, datetime):
        return "—"
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.strftime("%d.%m.%Y %H:%M")


def _status(value: str) -> str:
    style = "positive" if value in _SUCCESS else "negative" if value in _ERROR else "neutral"
    return f'<span class="tag tag-{style}">{_e(_STATUS.get(value, value))}</span>'


def _table(columns: tuple[str, ...], rows: list[tuple[str, ...]], message: str) -> str:
    if not rows:
        return (
            '<div class="empty"><span class="empty-mark">◇</span><strong>Пока пусто</strong>'
            f'<p>{_e(message)}</p></div>'
        )
    heads = "".join(f'<th scope="col">{_e(c)}</th>' for c in columns)
    cells = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return (
        '<div class="responsive-table" role="region" aria-label="Таблица данных" tabindex="0">'
        f'<table><thead><tr>{heads}</tr></thead><tbody>{cells}</tbody></table></div>'
    )


def _pagination(section: str, total: int, current: int, *, extra: str = "") -> str:
    total_pages = max(1, (total + 24) // 25)
    if total_pages <= 1:
        return f'<p class="pagination-hint">Всего записей: {total}</p>'
    previous = max(1, current - 1)
    following = min(total_pages, current + 1)
    base = f"/cabinet?section={section}" + extra
    return (
        '<div class="pagination">'
        f'<span>Страница {current} из {total_pages} · {total} записей</span>'
        f'<a class="button button-secondary" href="{base}&page={previous}"'
        f' {"aria-disabled=true tabindex=-1" if current <= 1 else ""}>← Назад</a>'
        f'<a class="button button-secondary" href="{base}&page={following}"'
        f' {"aria-disabled=true tabindex=-1" if current >= total_pages else ""}>Вперёд →</a>'
        '</div>'
    )


def _nav(active: str) -> str:
    previous_group = ""
    items = []
    for name, label, symbol, group in _SECTIONS:
        if group != previous_group:
            items.append(f'<div class="nav-caption">{_e(group)}</div>')
            previous_group = group
        items.append(
            f'<a href="/cabinet?section={name}" class="nav-item {"active" if name == active else ""}"'
            f' {"aria-current=page" if name == active else ""}>'
            f'<span class="nav-symbol" aria-hidden="true">{symbol}</span>{_e(label)}</a>'
        )
    return "".join(items)


def page_html(
    partner: Partner,
    active: str,
    body: str,
    csrf: str,
    *,
    notice: str = "",
) -> HTMLResponse:
    title = _TITLES.get(active, "Обзор")
    settings = get_settings()
    bot = settings.telegram_bot_username or "apineurobrobot"
    info = (
        '<div class="toast-success" role="status">'
        '<span aria-hidden="true">✓</span> Изменения сохранены</div>'
        if notice == "updated"
        else ""
    )
    html = (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="dark">'
        f'<title>{_e(title)} — Нейроныч API</title>'
        '<meta name="robots" content="noindex,nofollow">'
        '<link rel="icon" href="/ui/logo-mark.svg" type="image/svg+xml">'
        '<link rel="stylesheet" href="/ui/portal.css?v=1">'
        '<script src="/ui/portal.js?v=1" defer></script></head>'
        '<body class="portal-body">'
        '<a class="skip-link" href="#content">К содержимому</a>'
        '<div class="portal-shell">'
        '<aside class="portal-sidebar" id="portal-sidebar">'
        '<a class="portal-brand" href="/cabinet"><span class="brand-symbol">'
        '<img src="/ui/logo-mark.svg" width="34" height="34" alt=""></span>'
        '<span>нейроныч<small>API CONTROL PLANE</small></span></a>'
        '<div class="workspace-chip"><span class="workspace-icon">N</span><span>'
        f'<strong>{_e(partner.project_name)}</strong><small>Рабочее пространство</small></span>'
        '<span class="workspace-chevron">⌄</span></div>'
        f'<nav class="sidebar-nav" aria-label="Разделы кабинета">{_nav(active)}</nav>'
        '<div class="sidebar-bottom"><span class="online-dot"></span> API PLATFORM · ONLINE'
        f'<a href="https://t.me/{_e(bot)}" target="_blank" rel="noopener noreferrer">'
        'Telegram-поддержка ↗</a></div></aside>'
        '<div class="portal-main">'
        '<header class="portal-topbar">'
        '<div class="topbar-leading"><button id="menu-toggle" class="icon-button mobile-menu"'
        ' type="button" aria-controls="portal-sidebar" aria-expanded="false" aria-label="Открыть меню">☰</button>'
        '<span class="breadcrumb-root">Партнёрский кабинет</span>'
        f'<span class="breadcrumb-divider">/</span><span class="breadcrumb-active">{_e(title)}</span></div>'
        '<div class="topbar-actions">'
        '<span class="topbar-balance"><span>Баланс</span>'
        f'<strong>{_e(_rub(partner.balance_rub))}</strong></span>'
        '<a class="button button-primary topup-button" href="/cabinet?section=credits">+ Пополнить</a>'
        f'<span class="avatar" aria-label="{_e(partner.company_name)}">N</span></div></header>'
        f'<main id="content" class="portal-content">{info}{body}</main>'
        '<footer class="portal-footer"><span>© Нейроныч · API Platform</span>'
        '<a href="/docs">Документация</a><a href="/prices">Тарифы</a>'
        '<span>Все суммы в ₽</span></footer></div></div>'
        '<div id="menu-backdrop" class="menu-backdrop" hidden></div>'
        '</body></html>'
    )
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "private, no-store, max-age=0",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _heading(eyebrow: str, title: str, description: str = "", action: str = "") -> str:
    return (
        '<div class="page-heading"><div>'
        f'<p class="eyebrow">{_e(eyebrow)}</p><h1>{_e(title)}</h1>'
        f'<p class="page-description">{_e(description)}</p></div>{action}</div>'
    )


def _stats(cards: list[tuple[str, str, str]]) -> str:
    result = []
    for label, value, hint in cards:
        result.append(
            f'<div class="metric-card"><span class="metric-label">{_e(label)}</span>'
            f'<strong class="metric-value">{_e(value)}</strong><small>{_e(hint)}</small></div>'
        )
    return '<div class="metric-grid">' + "".join(result) + "</div>"


def _bars(daily: list[tuple[str, int, Decimal]], *, metric: str) -> str:
    dates = [datetime.now(UTC).date() - timedelta(days=offset) for offset in range(13, -1, -1)]
    records = {day: (number, amount) for day, number, amount in daily}
    amounts = [
        Decimal(records.get(day.isoformat(), (0, Decimal("0")))[0 if metric == "requests" else 1])
        for day in dates
    ]
    peak = max(amounts, default=Decimal("0")) or Decimal("1")
    elements = []
    for day, amount in zip(dates, amounts, strict=True):
        percent = max(Decimal("2"), (amount / peak) * 100)
        label = str(int(amount)) if metric == "requests" else _rub(amount)
        elements.append(
            f'<div class="chart-column" title="{_e(day.isoformat())}: {_e(label)}">'
            f'<span class="chart-bar" style="height:{percent:.1f}%"></span>'
            f'<span class="chart-tick">{day.strftime("%d.%m")}</span></div>'
        )
    return (
        '<div class="chart" role="img" aria-label="Активность за последние 14 дней">'
        + "".join(elements) + "</div>"
    )


def _generation_rows(gens: list[Generation], *, details: bool = True) -> list[tuple[str, ...]]:
    result = []
    for gen in gens:
        identifier = (
            f'<a href="/cabinet?section=logs&generation={quote(gen.id)}" class="mono-link">'
            f'{_e(gen.id[:8])}…</a>'
            if details
            else f'<code>{_e(gen.id[:8])}</code>'
        )
        result.append((
            identifier,
            f'<span class="model-name">{_e(gen.model_slug)}</span>',
            _status(gen.status),
            _e(_rub(gen.actual_charge_rub)) if gen.actual_charge_rub is not None else "—",
            _e(_dt(gen.created_at)),
        ))
    return result


def overview_view(partner: Partner, usage: dict, recent: list[Generation], keys_count: int) -> str:
    title = _heading(
        "РАБОЧЕЕ ПРОСТРАНСТВО / OVERVIEW",
        "Обзор использования",
        "Контролируйте запросы, расходы и доступ к API в одном месте.",
        '<a class="button button-primary" href="/cabinet?section=keys">+ Создать API-ключ</a>',
    )
    cards = _stats([
        ("Доступный баланс", _rub(partner.available_balance_rub), "С учётом кредитного лимита"),
        ("Расход за 30 дней", _rub(usage["spent"]), "Фактические списания"),
        ("Запросов за 30 дней", str(usage["requests"]), f'Успешно: {usage["completed"]}'),
        ("Активных API-ключей", str(keys_count), "Управление доступом"),
    ])
    chart = (
        '<section class="surface chart-surface"><div class="section-top">'
        '<div><h2>Динамика запросов</h2><p>Последние 14 дней</p></div>'
        '<a href="/cabinet?section=activity" class="text-link">Подробная аналитика ↗</a></div>'
        + _bars(usage["daily"], metric="requests") + "</section>"
    )
    log_rows = _generation_rows(recent)
    recent_html = (
        '<section class="surface"><div class="section-top"><div><h2>Последние запросы</h2>'
        '<p>Состояние задач и реальные списания</p></div>'
        '<a class="text-link" href="/cabinet?section=logs">Все запросы ↗</a></div>'
        + _table(
            ("ID запроса", "Модель", "Статус", "Списано", "Дата UTC"),
            log_rows, "Запросы появятся после первой генерации.",
        )
        + "</section>"
    )
    return title + cards + chart + recent_html


def activity_view(usage: dict) -> str:
    period = usage["days"]
    days_links = "".join(
        f'<a class="period-tab {"selected" if days == period else ""}" href="/cabinet?section=activity&days={days}">'
        f'{days} дней</a>' for days in (7, 30, 90)
    )
    title = _heading(
        "УСЛУГИ / ANALYTICS", "Аналитика",
        "Траты и объём API-запросов по моделям, за выбранный период.",
        f'<div class="period-tabs" aria-label="Период">{days_links}</div>',
    )
    cards = _stats([
        ("Запросов", str(usage["requests"]), f"Последние {period} дней"),
        ("Списано", _rub(usage["spent"]), "Рублёвый биллинг"),
        ("Успешных", str(usage["completed"]), "Завершено без ошибки"),
        ("Неуспешных", str(usage["failed"]), "Ошибки и отмены"),
    ])
    charts = (
        '<div class="analytics-grid"><section class="surface chart-surface">'
        '<div class="section-top"><div><h2>Объём запросов</h2><p>По дням · 14 последних дней</p></div></div>'
        + _bars(usage["daily"], metric="requests")
        + '</section><section class="surface chart-surface"><div class="section-top">'
        '<div><h2>Расходы</h2><p>Фактические списания, ₽</p></div></div>'
        + _bars(usage["daily"], metric="spent") + "</section></div>"
    )
    rows = [
        (_e(model), _e(count), f'<strong>{_e(_rub(spent))}</strong>')
        for model, count, spent in usage["models"]
    ]
    return (
        title + cards + charts + '<section class="surface"><div class="section-top"><div>'
        '<h2>Использование по моделям</h2><p>Модели с наибольшими расходами</p></div>'
        '<a class="text-link" href="/cabinet/export/logs">Экспорт CSV ↧</a></div>'
        + _table(("Модель", "Запросов", "Расход"), rows, "Данных за этот период пока нет.")
        + "</section>"
    )


def logs_view(
    gens: list[Generation],
    total: int,
    current: int,
    selected_status: str,
    selected_model: str,
    detail: Generation | None,
) -> str:
    title = _heading(
        "УСЛУГИ / LOGS", "Журнал запросов",
        "История генераций с фильтрами и фактической стоимостью.",
        '<a class="button button-secondary" href="/cabinet/export/logs">↓ Экспорт CSV</a>',
    )
    options = [("", "Любой статус"), ("queued", "В очереди"), ("processing", "В работе"),
               ("completed", "Завершено"), ("failed", "Ошибка"), ("cancelled", "Отменено")]
    select_status = "".join(
        f'<option value="{_e(v)}" {"selected" if v == selected_status else ""}>{_e(label)}</option>'
        for v, label in options
    )
    filters = (
        '<form class="filterbar" method="get" action="/cabinet">'
        '<input type="hidden" name="section" value="logs">'
        '<label>Модель<input type="search" name="model" maxlength="80"'
        f' placeholder="Например, kling..." value="{_e(selected_model)}"></label>'
        f'<label>Статус<select name="status">{select_status}</select></label>'
        '<button class="button button-secondary" type="submit">Применить</button>'
        '<a class="text-link" href="/cabinet?section=logs">Сбросить</a></form>'
    )
    detail_html = ""
    if detail is not None:
        prompt = detail.prompt[:1800] if detail.prompt else ""
        detail_html = (
            '<section class="surface detail-surface"><div class="section-top"><div>'
            f'<h2>Запрос {_e(detail.id)}</h2><p>{_e(detail.model_slug)} · {_e(detail.mode)}</p></div>'
            '<a class="text-link" href="/cabinet?section=logs">Закрыть ✕</a></div>'
            '<div class="detail-grid">'
            f'<div><span>Статус</span>{_status(detail.status)}</div>'
            f'<div><span>Разрешение</span><strong>{_e(detail.resolution)}</strong></div>'
            f'<div><span>Длительность</span><strong>{_e(detail.duration_seconds)} с</strong></div>'
            f'<div><span>Списано</span><strong>{_e(_rub(detail.actual_charge_rub))}</strong></div></div>'
            '<h3>Запрос</h3>'
            f'<pre class="prompt-preview">{_e(prompt)}</pre>'
            f'<p class="small-muted">Создан: {_e(_dt(detail.created_at))} UTC'
            f' · Завершён: {_e(_dt(detail.client_finalized_at))} UTC</p></section>'
        )
    return (
        title + detail_html + '<section class="surface">' + filters
        + _table(
            ("ID запроса", "Модель", "Статус", "Списано", "Дата UTC"),
            _generation_rows(gens),
            "Запросов с такими фильтрами нет.",
        )
        + _pagination(
            "logs", total, current,
            extra=f"&status={quote(selected_status)}&model={quote(selected_model)}",
        )
        + "</section>"
    )


async def models_view(db, tariff_rows: list, search: str) -> str:
    title = _heading(
        "КАТАЛОГ / MODELS", "Модели и тарифы",
        "Только модели, доступные вашему аккаунту. Все тарифы показаны в рублях.",
    )
    filters = (
        '<form class="filterbar" action="/cabinet" method="get">'
        '<input type="hidden" name="section" value="models">'
        '<label class="grow">Поиск по модели<input type="search" name="q"'
        f' placeholder="Найти модель..." maxlength="80" value="{_e(search)}"></label>'
        '<button class="button button-secondary" type="submit">Найти</button></form>'
    )
    needs_live_fx = any(
        model.slug == EDIT_MODEL and rate.mode == EDIT_MODE
        and snapshot is None and edit_markup(model.slug) is not None
        for model, rate, snapshot in tariff_rows
    )
    fx = Decimal("1")
    if needs_live_fx:
        from app.billing.fx import current_fx

        try:
            fx = (await current_fx(db))["rate"]
        except HTTPException:
            fx = Decimal("0")
    rows = []
    for model, rate, snapshot in tariff_rows:
        if snapshot is not None:
            price = _rub(snapshot.price_rub)
        elif model.slug == EDIT_MODEL and rate.mode == EDIT_MODE and edit_markup(model.slug) is not None and fx <= 0:
            price = "По запросу"
        else:
            price = _rub(retail_rate(model.slug, rate, fx))
        unit = {"generation": "за генерацию", "second": "за секунду", "million_tokens": "за млн токенов"}
        rows.append((
            f'<strong class="model-name">{_e(model.name)}</strong><small class="model-id">{_e(model.slug)}</small>',
            _e({"llm": "Текст", "image": "Изображение", "video": "Видео"}.get(model.modality, model.modality)),
            f'<code>{_e(rate.mode)}</code> · {_e(rate.resolution)}',
            f'<strong>{_e(price)}</strong>'
            f'<small class="model-id">{_e(unit.get(rate.billing_unit, rate.billing_unit))}</small>',
            '<span class="tag tag-positive">Доступна</span>',
        ))
    return (
        title + '<section class="surface">' + filters
        + '<p class="table-intro">Цены зависят от режима и настроек запроса. '
        'Фактическое списание отображается в журнале запросов.</p>'
        + _table(("Модель", "Категория", "Режим", "Тариф", "Доступ"), rows, "Доступных моделей по запросу не найдено.")
        + '</section><p class="footnote">Тарификация и валюта рассчитываются на сервере по действующим правилам. '
        'Цены поставщика и внутренние закупочные лимиты не отображаются.</p>'
    )


def _key_actions(key: ApiKey, csrf: str) -> str:
    if not key.is_active:
        return '<span class="small-muted">Отключён</span>'
    return (
        '<div class="key-actions"><details><summary class="text-link">Webhook</summary>'
        '<form method="post" action="/cabinet/action" class="inline-form">'
        '<input type="hidden" name="action" value="set_webhook">'
        f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
        f'<input type="hidden" name="key_id" value="{_e(key.id)}">'
        '<label>HTTPS webhook URL'
        f'<input type="url" name="webhook_url" value="{_e(key.webhook_url or "")}"'
        ' placeholder="https://example.com/webhook" maxlength="2048"></label>'
        '<label>Секрет подписи (новый, необязательно)'
        '<input type="password" name="webhook_secret" minlength="16" maxlength="4096"'
        ' autocomplete="new-password" placeholder="Не менее 16 символов"></label>'
        '<button class="button button-secondary" type="submit">Сохранить</button></form></details>'
        '<form method="post" action="/cabinet/action" data-confirm="Отозвать этот API-ключ? Это действие необратимо.">'
        '<input type="hidden" name="action" value="revoke_key">'
        f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
        f'<input type="hidden" name="key_id" value="{_e(key.id)}">'
        '<button class="button-link-danger" type="submit">Отозвать</button></form></div>'
    )


def keys_view(keys: list[ApiKey], csrf: str) -> str:
    title = _heading(
        "УПРАВЛЕНИЕ / API KEYS",
        "API-ключи",
        "Создавайте отдельные ключи для разных интеграций и отзывайте скомпрометированные.",
    )
    form = (
        '<section class="surface action-surface"><div><h2>Создать новый ключ</h2>'
        '<p>Полное значение показывается только один раз. Сохраните его в менеджере секретов.</p></div>'
        '<form method="post" action="/cabinet/action" class="create-key-form">'
        '<input type="hidden" name="action" value="create_key">'
        f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
        '<label>Название ключа<input type="text" name="name" minlength="2" maxlength="120"'
        ' required placeholder="Production backend"></label>'
        '<button class="button button-primary" type="submit">+ Создать ключ</button></form></section>'
    )
    rows = [
        (
            f'<strong>{_e(key.name)}</strong><small class="model-id">{_e(_dt(key.created_at))} UTC</small>',
            f'<code>{_e(key.key_prefix)}••••••</code>',
            _status("completed") if key.is_active else _status("cancelled"),
            _e(key.webhook_url or "Не настроен"),
            _key_actions(key, csrf),
        )
        for key in keys
    ]
    return (
        title + form + '<section class="surface"><div class="section-top"><div>'
        '<h2>Ваши ключи</h2><p>Секреты не хранятся в интерфейсе.</p></div>'
        f'<span class="counter">{sum(bool(key.is_active) for key in keys)} активных</span></div>'
        + _table(("Название", "Префикс", "Активность", "Webhook", "Действия"), rows, "Создайте первый API-ключ.")
        + "</section>"
    )


def credits_view(partner: Partner, invoices: list[PaymentInvoice], csrf: str, idempotency: str) -> str:
    title = _heading(
        "БИЛЛИНГ / CREDITS", "Баланс и пополнение",
        "Без скрытых списаний: история всех операций доступна в кабинете.",
    )
    stats = _stats([
        ("Баланс", _rub(partner.balance_rub), "Доступные средства"),
        ("Кредитный лимит", _rub(partner.credit_limit_rub), "Установлен администратором"),
        ("Доступно", _rub(partner.available_balance_rub), "Баланс + кредитный лимит"),
        ("Покрытие себестоимости", _rub(partner.cost_coverage_rub), "Расчётная величина"),
    ])
    form = (
        '<section class="surface action-surface"><div><h2>Пополнить баланс</h2>'
        '<p>Создайте счёт через подключённого платёжного провайдера. '
        'Зачисление выполняется только после подтверждения оплаты.</p></div>'
        '<form class="topup-form" method="post" action="/cabinet/action">'
        '<input type="hidden" name="action" value="create_invoice">'
        f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
        f'<input type="hidden" name="idempotency_key" value="{_e(idempotency)}">'
        '<label>Сумма, ₽ <input type="number" name="requested_rub" min="1000" step="1"'
        ' value="1000" required></label>'
        '<label>Email для чека<input type="email" name="buyer_email"'
        f' value="{_e(partner.payment_email or "")}" placeholder="email@example.com" autocomplete="email"></label>'
        '<button type="submit" class="button button-primary">Создать счёт →</button>'
        '</form></section>'
    )
    rows = []
    for inv in invoices:
        link = ""
        if inv.invoice_url and inv.invoice_url.startswith("https://"):
            link = (
                f'<a class="text-link" href="{_e(inv.invoice_url)}"'
                ' target="_blank" rel="noopener noreferrer">Оплатить ↗</a>'
            )
        rows.append((
            _e(_dt(inv.created_at)),
            f'<strong>{_e(_rub(inv.requested_rub))}</strong>',
            _status(inv.status),
            link,
        ))
    return (
        title + stats + form + '<section class="surface"><div class="section-top"><div>'
        '<h2>Недавние счета</h2><p>Обновите страницу для актуального статуса.</p></div>'
        '<a class="text-link" href="/cabinet?section=invoices">Все счета ↗</a></div>'
        + _table(("Создан", "Сумма", "Статус", "Оплата"), rows, "Счетов пока нет.")
        + "</section>"
    )


def invoices_view(items: list[PaymentInvoice], total: int, current: int, selected_status: str, csrf: str) -> str:
    title = _heading(
        "БИЛЛИНГ / INVOICES", "Счета",
        "История выставления счетов и статусы поступления средств.",
        '<a class="button button-primary" href="/cabinet?section=credits">+ Новый счёт</a>',
    )
    rows = []
    for inv in items:
        link = ""
        if inv.invoice_url and inv.invoice_url.startswith("https://"):
            link = (
                f'<a class="text-link" target="_blank" rel="noopener noreferrer"'
                f' href="{_e(inv.invoice_url)}">Перейти к оплате ↗</a>'
            )
        cancel = ""
        if inv.status == "active":
            cancel = (
                '<form method="post" action="/cabinet/action" data-confirm="Отменить этот счёт?">'
                '<input type="hidden" name="action" value="cancel_invoice">'
                f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
                f'<input type="hidden" name="payment_id" value="{_e(inv.id)}">'
                '<button type="submit" class="button-link-danger">Отменить</button></form>'
            )
        rows.append((
            f'<code>{_e(inv.id[:8])}…</code><small class="model-id">{_e(_dt(inv.created_at))} UTC</small>',
            _e(inv.provider),
            f'<strong>{_e(_rub(inv.requested_rub))}</strong>',
            _status(inv.status),
            link + cancel,
        ))
    filters = (
        '<form action="/cabinet" method="get" class="filterbar">'
        '<input type="hidden" name="section" value="invoices">'
        '<label>Статус<select name="status">'
        + "".join(
            f'<option value="{_e(v)}" {"selected" if v == selected_status else ""}>{_e(t)}</option>'
            for v, t in (
                ("", "Все"), ("active", "Ожидает оплаты"), ("paid", "Оплачен"),
                ("credited", "Зачислен"), ("expired", "Истёк"), ("refunded", "Возвращён"),
            )
        )
        + '</select></label><button class="button button-secondary">Фильтровать</button>'
        '<a class="text-link" href="/cabinet/export/invoices">↓ Экспорт CSV</a></form>'
    )
    return (
        title + '<section class="surface">' + filters
        + _table(("Счёт", "Провайдер", "Сумма", "Статус", "Действия"), rows, "Счета ещё не создавались.")
        + _pagination("invoices", total, current, extra=f"&status={quote(selected_status)}")
        + "</section>"
    )


def ledger_view(items: list[LedgerEntry], total: int, current: int, selected_type: str) -> str:
    title = _heading(
        "БИЛЛИНГ / LEDGER", "Операции по балансу",
        "Неизменяемая история списаний, пополнений и корректировок.",
        '<a class="button button-secondary" href="/cabinet/export/ledger">↓ Экспорт CSV</a>',
    )
    rows = [
        (
            _e(_dt(item.created_at)),
            _e({
                "generation_charge": "Списание за генерацию",
                "payment_credit": "Пополнение",
                "manual_adjustment": "Корректировка",
                "payment_refund_adjustment": "Возврат",
            }.get(item.operation_type, item.operation_type)),
            f'<strong class="{"negative-money" if item.amount_rub < 0 else "positive-money"}">'
            f'{_e(_rub(item.amount_rub))}</strong>',
            _e(_rub(item.balance_after_rub)),
        )
        for item in items
    ]
    form = (
        '<form action="/cabinet" method="get" class="filterbar">'
        '<input type="hidden" name="section" value="ledger">'
        '<label>Тип операции<select name="type">'
        + "".join(
            f'<option value="{_e(v)}" {"selected" if v == selected_type else ""}>{_e(label)}</option>'
            for v, label in (
                ("", "Все операции"), ("generation_charge", "Списания"),
                ("payment_credit", "Пополнения"), ("manual_adjustment", "Корректировки"),
                ("payment_refund_adjustment", "Возвраты"),
            )
        )
        + '</select></label><button class="button button-secondary">Применить</button></form>'
    )
    return (
        title + '<section class="surface">' + form
        + _table(("Дата UTC", "Операция", "Сумма", "Баланс после"), rows, "Операций ещё нет.")
        + _pagination("ledger", total, current, extra=f"&type={quote(selected_type)}")
        + "</section>"
    )


def settings_view(partner: Partner, csrf: str) -> str:
    title = _heading(
        "АККАУНТ / SETTINGS", "Настройки",
        "Данные партнёра, контактная информация и безопасность сессии.",
    )
    profile = (
        '<section class="surface settings-card"><div class="section-top"><div>'
        '<h2>Профиль организации</h2><p>Партнёрский аккаунт</p></div>'
        '<span class="tag tag-positive">Активен</span></div>'
        '<div class="detail-grid">'
        f'<div><span>Компания</span><strong>{_e(partner.company_name)}</strong></div>'
        f'<div><span>Проект</span><strong>{_e(partner.project_name)}</strong></div>'
        f'<div><span>Partner ID</span><code>{_e(partner.id)}</code></div>'
        f'<div><span>Telegram ID</span><code>{_e(partner.telegram_id)}</code></div>'
        '</div></section>'
    )
    email = (
        '<section class="surface settings-card"><h2>Платёжные данные</h2>'
        '<p>Адрес для чеков и уведомлений о пополнении.</p>'
        '<form class="settings-form" method="post" action="/cabinet/action">'
        '<input type="hidden" name="action" value="update_email">'
        f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
        '<label>Email для чеков<input name="buyer_email" type="email" maxlength="320"'
        f' value="{_e(partner.payment_email or "")}" placeholder="billing@example.com"></label>'
        '<button type="submit" class="button button-primary">Сохранить</button></form></section>'
    )
    security = (
        '<section class="surface settings-card"><h2>Безопасность</h2>'
        '<p>Вход — через подтверждённый Telegram-аккаунт. '
        'Сеанс хранится на сервере и может быть закрыт в любой момент.</p>'
        '<form method="post" action="/cabinet/logout">'
        f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
        '<button type="submit" class="button button-secondary">Выйти из аккаунта</button></form></section>'
    )
    return title + profile + email + security


def secret_page(partner: Partner, csrf: str, token: str) -> HTMLResponse:
    """A no-store one-time key response; never persisted to localStorage."""
    body = (
        '<div class="secret-container"><span class="eyebrow">УПРАВЛЕНИЕ / API KEYS</span>'
        '<div class="secret-icon">✓</div><h1>API-ключ создан</h1>'
        '<p>Скопируйте и сохраните ключ сейчас. Повторно его посмотреть нельзя.</p>'
        '<div class="secret-field"><code id="new-api-key">' + _e(token) + '</code>'
        '<button type="button" class="button button-primary" data-copy-target="new-api-key">'
        'Копировать</button></div>'
        '<p class="warning-note">Не отправляйте ключ в чаты, не вставляйте в браузерные приложения '
        'и не добавляйте в Git.</p>'
        '<a href="/cabinet?section=keys" class="button button-secondary">Вернуться к API-ключам →</a>'
        '</div>'
    )
    return page_html(partner, "keys", body, csrf)
