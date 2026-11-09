"""Accessible server-rendered partner cabinet over the existing financial source of truth."""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html import escape

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.models import ApiKey, Partner
from app.billing.history import final_generation_rows, partner_transactions
from app.catalog.models import Model
from app.generations.models import Generation
from app.payments.models import PaymentInvoice

SECTIONS = {
    "overview": ("Обзор", "◫"),
    "generations": ("Генерации", "▤"),
    "transactions": ("Операции", "⇄"),
    "payments": ("Счета", "▧"),
    "keys": ("API-ключи", "⌘"),
    "models": ("Модели", "◈"),
    "integration": ("Интеграция", "⌁"),
    "settings": ("Настройки", "⚙"),
}

TG_LINK = "https://t.me/apineurobrobot"


def _e(value: object) -> str:
    return escape(str(value if value is not None else ""), quote=True)


def _rub(value: Decimal | int | None) -> str:
    if value is None:
        return "—"
    return f"{Decimal(value):,.2f}".replace(",", " ") + " ₽"


def _when(value: object) -> str:
    if not isinstance(value, datetime):
        return "—"
    return value.strftime("%d.%m.%Y · %H:%M") + " UTC"


def _empty(message: str) -> str:
    return f'<div class="empty-state"><span class="empty-symbol">◌</span><p>{_e(message)}</p></div>'


def _table(headers: list[str], rows: list[list[str]], empty: str) -> str:
    if not rows:
        return _empty(empty)
    head = "".join(f"<th scope='col'>{_e(item)}</th>" for item in headers)
    body = "".join("<tr>" + "".join(f"<td>{value}</td>" for value in row) + "</tr>" for row in rows)
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _pill(value: str) -> str:
    known = {
        "completed": ("Готово", "good"),
        "done": ("Готово", "good"),
        "active": ("Активен", "good"),
        "paid": ("Оплачен", "good"),
        "credited": ("Зачислен", "good"),
        "cancelled": ("Отменён", "muted"),
        "failed": ("Ошибка", "bad"),
        "queued": ("В очереди", "pending"),
        "pending": ("Ожидает", "pending"),
        "processing": ("Выполняется", "pending"),
    }
    text, cls = known.get(value, (value.replace("_", " "), "muted"))
    return f'<span class="status status-{cls}">{_e(text)}</span>'


def render_login(bot_username: str | None) -> str:
    username = (bot_username or "").strip().lstrip("@")
    if username:
        widget = (
            '<div class="widget-holder" id="telegram-widget">'
            '<script async src="https://telegram.org/js/telegram-widget.js?22" '
            f'data-telegram-login="{_e(username)}" data-size="large" '
            'data-radius="8" data-request-access="write" '
            'data-onauth="onTelegramAuth(user)"></script></div>'
        )
        subtitle = "Войдите через Telegram, который привязан к вашему партнёрскому аккаунту."
    else:
        widget = '<p class="notice">Вход пока не настроен. Администратору необходимо указать имя Telegram-бота.</p>'
        subtitle = "Для доступа нужен активный партнёрский аккаунт."
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0b0e14">
<title>Вход · Нейроныч API</title>
<link rel="icon" href="/ui/logo-mark.svg" type="image/svg+xml">
<link rel="stylesheet" href="/cabinet/assets/cabinet.css">
<script defer src="/cabinet/assets/cabinet-login.js"></script>
</head><body class="login-page">
<main class="login-wrap"><div class="login-upper"><a class="brand" href="/">
<span class="brand-mark">Н</span><span>НЕЙРОНЫЧ <small>API PLATFORM</small></span></a>
<a class="login-back" href="/">← На сайт</a></div>
<section class="login-card"><span class="section-kicker">ЛИЧНЫЙ КАБИНЕТ</span>
<h1>Всё под<br><em>контролем.</em></h1>
<p>{_e(subtitle)}</p>
{widget}
<p class="login-feedback" id="login-feedback" role="status" aria-live="polite"></p>
<div class="login-rule"></div>
<p class="login-help">Нет аккаунта? 
<a href="{TG_LINK}?start=apply" target="_blank" rel="noopener noreferrer">Подайте заявку в Telegram ↗</a></p>
</section><footer class="login-foot"><span>Нейроныч · API</span>
<a href="/guide">Документация</a><span>Безопасный вход</span></footer></main>
</body></html>"""


def _shell(partner: Partner, active: str, content: str) -> str:
    items = "".join(
        f'<a href="/cabinet/{slug}" class="side-link {"selected" if active == slug else ""}" '
        f'{"aria-current=page" if active == slug else ""}>'
        f'<span class="side-icon">{glyph}</span><span>{_e(title)}</span></a>'
        for slug, (title, glyph) in SECTIONS.items()
    )
    title = SECTIONS[active][0]
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0b0e14">
<title>{_e(title)} · Нейроныч API</title>
<link rel="icon" href="/ui/logo-mark.svg" type="image/svg+xml">
<link rel="stylesheet" href="/cabinet/assets/cabinet.css">
</head><body class="cabinet-page">
<a class="skip-link" href="#content">К содержимому</a>
<div class="workspace"><aside class="sidebar">
<a href="/cabinet/overview" class="brand brand-sidebar"><span class="brand-mark">Н</span>
<span>НЕЙРОНЫЧ <small>API PLATFORM</small></span></a>
<div class="nav-group-label">РАБОЧЕЕ ПРОСТРАНСТВО</div>
<nav class="side-nav" aria-label="Разделы кабинета">{items}</nav>
<div class="sidebar-foot"><span class="account-bubble">{_e(partner.project_name[:1].upper())}</span>
<div class="account-details"><strong>{_e(partner.project_name)}</strong>
<small>{_e(partner.company_name)}</small></div></div>
</aside><div class="content-area">
<header class="topbar"><div class="breadcrumb">Личный кабинет <span>/</span> {_e(title)}</div>
<div class="topbar-right"><span class="top-status"><i></i> Партнёрский аккаунт</span>
<form action="/cabinet/logout" method="post">
<button type="submit" class="text-button">Выйти ↗</button></form></div></header>
<main id="content" tabindex="-1" class="page-body">{content}</main>
<footer class="page-footer"><span>© Нейроныч API</span>
<span><a href="/guide">Документация</a> · <a href="/price">Цены</a> · 
<a href="{TG_LINK}" target="_blank" rel="noopener noreferrer">Поддержка ↗</a></span>
</footer></div></div></body></html>"""


def _heading(eyebrow: str, title: str, description: str, action: str = "") -> str:
    return (
        f'<div class="page-heading"><div><div class="section-kicker">{_e(eyebrow)}</div>'
        f'<h1>{_e(title)}</h1><p>{_e(description)}</p></div>{action}</div>'
    )


def _action(label: str, href: str, *, secondary: bool = False) -> str:
    return (
        f'<a class="action {"action-secondary" if secondary else ""}" href="{_e(href)}'
        f'">{_e(label)} <span>↗</span></a>'
    )


async def _overview(db: AsyncSession, partner: Partner) -> str:
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=30)
    recent_cutoff = now - timedelta(days=13)
    generations = (
        await db.execute(
            select(Generation)
            .where(Generation.partner_id == partner.id, Generation.created_at >= cutoff)
            .order_by(Generation.created_at.desc())
            .limit(8)
        )
    ).scalars().all()
    statuses = (
        await db.execute(
            select(Generation.status, func.count())
            .where(Generation.partner_id == partner.id, Generation.created_at >= cutoff)
            .group_by(Generation.status)
        )
    ).all()
    count = sum(row[1] for row in statuses)
    succeeded = sum(row[1] for row in statuses if row[0] == "completed")
    finalized = final_generation_rows(partner.id).subquery()
    charge_rows = (
        await db.execute(
            select(func.date(finalized.c.created_at), func.sum(finalized.c.amount_rub))
            .where(finalized.c.created_at >= recent_cutoff)
            .group_by(func.date(finalized.c.created_at))
            .order_by(func.date(finalized.c.created_at))
        )
    ).all()
    month_debits = (
        await db.scalar(
            select(func.coalesce(func.sum(finalized.c.amount_rub), 0)).where(finalized.c.created_at >= cutoff)
        )
    ) or Decimal("0")
    chart_values: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for day, total in charge_rows:
        chart_values[str(day)] = -Decimal(total)
    series = [
        (now.date() - timedelta(days=offset)).isoformat()
        for offset in reversed(range(14))
    ]
    maximum = max([chart_values[day] for day in series] + [Decimal("0")])
    svg_bars = ""
    for idx, day in enumerate(series):
        value = chart_values[day]
        height = int(value / maximum * 144) if maximum > 0 else 0
        x = idx * 33 + 9
        svg_bars += (
            f'<rect x="{x}" y="{150-height}" width="18" height="{height}" rx="4"><title>'
            f'{_e(day)}: {_e(_rub(value))}</title></rect>'
        )
    ticks = "".join(
        f'<text x="{idx * 33 + 18}" y="169" text-anchor="middle">{day[-2:]}</text>'
        for idx, day in enumerate(series)
    )
    chart = (
        '<div class="chart-area" role="img" aria-label="Списания за последние 14 дней, в рублях">'
        '<svg viewBox="0 0 470 178" preserveAspectRatio="xMidYMid meet" aria-hidden="true">'
        f'<g class="chart-bars">{svg_bars}</g><g class="chart-labels">{ticks}</g></svg></div>'
    )
    latest = _table(
        ["Время", "Модель", "Статус", "Списание"],
        [
            [_e(_when(g.created_at)), _e(g.model_slug), _pill(g.status),
             _e(_rub(g.actual_charge_rub)) if g.status == "completed" else "—"]
            for g in generations
        ],
        "Запросов пока нет. Создайте первый запрос через API.",
    )
    conversion = round(succeeded * 100 / count) if count else 0
    return (
        _heading("ПАНЕЛЬ УПРАВЛЕНИЯ", f"Здравствуйте, {partner.project_name}.",
                 "Всё важное по вашему API в одном месте.", _action("Документация", "/guide", secondary=True))
        + '<div class="metrics-grid">'
        + f'<article class="metric metric-primary"><span>Текущий баланс</span><strong>'
          f'{_e(_rub(partner.balance_rub))}</strong><small>Доступно с кредитным лимито'
          f'м: {_e(_rub(partner.available_balance_rub))}</small></article>'
        + f'<article class="metric"><span>Расходы за 30 дней</span><strong>{_e(_rub(-Decimal(month_debits)))}'
          f'</strong><small>Только финализированные списания</small></article>'
        + f'<article class="metric"><span>Запросов за 30 дней</span><strong>{count:,}<'
          f'/strong><small>Все статусы</small></article>'
        + f'<article class="metric"><span>Успешных запросов</span><strong>{conversion}'
          f'%</strong><small>{succeeded} из {count} за 30 дней</small></article></div>'
        + '<div class="dashboard-grid"><section class="panel chart-panel">'
        + '<div class="panel-head"><div><h2>Динамика расходов</h2><p>14 дней · рубли</p></div></div>'
        + chart
        + '</section><section class="panel quick-panel"><div class="panel-head"><h2>Быстрые действия</h2></div>'
        + '<div class="quick-links">'
        + '<a href="/cabinet/keys"><span>⌘</span><span>Мои API-ключи<small>Управление'
          ' доступами</small></span><b>↗</b></a>'
        + '<a href="/cabinet/payments"><span>▧</span><span>Баланс и счета<small>Попол'
          'нение и оплаты</small></span><b>↗</b></a>'
        + '<a href="/cabinet/integration"><span>⌁</span><span>Подключить API<small>Пр'
          'имеры и документация</small></span><b>↗</b></a>'
        + '</div></section></div>'
        + '<section class="panel recent-panel"><div class="panel-head"><div><h2>После'
          'дние генерации</h2><p>Недавняя активность вашего проекта</p></div>'
        + '<a class="panel-link" href="/cabinet/generations">Все запросы →</a></div>'
        + latest + '</section>'
    )


async def _generations(db: AsyncSession, partner: Partner) -> str:
    rows = (
        await db.execute(
            select(Generation)
            .where(Generation.partner_id == partner.id)
            .order_by(Generation.created_at.desc())
            .limit(40)
        )
    ).scalars().all()
    return (
        _heading("ИСТОРИЯ", "Генерации", "Последние 40 запросов. Показываются только данные вашего аккаунта.")
        + '<section class="panel table-panel">'
        + _table(["Дата", "ID запроса", "Модель", "Статус", "Начислено / списано"],
                 [[_e(_when(g.created_at)),
                   f'<code title="{_e(g.id)}">{_e(g.id[:12])}…</code>',
                   _e(g.model_slug), _pill(g.status),
                   _e(_rub(g.actual_charge_rub)) if g.status == "completed" else "—"]
                  for g in rows],
                 "Вы ещё не отправляли запросы.")
        + '</section><p class="context-note">Сумма показывается только для успешных з'
          'апросов с известной фактической оплатой. '
        'Незавершённые резервирования не считаются итоговым расходом.</p>'
    )


async def _transactions(db: AsyncSession, partner: Partner) -> str:
    rows = await partner_transactions(db, partner.id, page=0)
    labels = {
        "generation_charge": "Генерация",
        "payment_credit": "Пополнение",
        "payment_refund_adjustment": "Корректировка возврата",
        "manual_adjustment": "Корректировка баланса",
    }
    return (
        _heading("ФИНАНСЫ", "Операции", "Завершённые списания и изменения баланса без промежуточных резервов.")
        + '<section class="panel table-panel">'
        + _table(["Дата", "Операция", "Модель", "Сумма"],
                 [[_e(_when(t.created_at)), _e(labels.get(t.operation_type, t.operation_type)),
                   _e(t.model_slug or "—"), _e(_rub(t.amount_rub))] for t in rows],
                 "Финансовых операций пока нет.")
        + '</section><p class="context-note">Показаны последние 8 операций. Полную ис'
          'торию можно получить в Telegram-кабинете.</p>'
    )


async def _payments(db: AsyncSession, partner: Partner) -> str:
    invoices = (
        await db.execute(
            select(PaymentInvoice)
            .where(PaymentInvoice.partner_id == partner.id)
            .order_by(PaymentInvoice.created_at.desc())
            .limit(40)
        )
    ).scalars().all()
    return (
        _heading("ФИНАНСЫ", "Счета и пополнения",
                 "Баланс общий для всех API-ключей. Новый счёт можно выставить через Telegram.",
                 _action("Пополнить в Telegram", TG_LINK))
        + f'<div class="wallet-strip"><div><span>Баланс</span><strong>{_e(_rub(partner.balance_rub))}</strong></div>'
        + f'<div><span>Кредитный лимит</span><strong>{_e(_rub(partner.credit_limit_rub))}</strong></div>'
        + f'<div><span>Доступно</span><strong>{_e(_rub(partner.available_balance_rub))}</strong></div></div>'
        + '<section class="panel table-panel"><div class="panel-head"><h2>Последние счета</h2></div>'
        + _table(["Дата", "Счёт", "К зачислению", "Статус"],
                 [[_e(_when(p.created_at)),
                   f'<code>{_e(p.id[:12])}…</code>',
                   _e(_rub(p.requested_rub)), _pill(p.status)] for p in invoices],
                 "Нет выставленных счетов.")
        + '</section><p class="context-note">Фактическое зачисление подтверждается платёжным провайдером. '
        'Не оплачивайте повторно один и тот же счёт при задержке подтверждения.</p>'
    )


async def _keys(db: AsyncSession, partner: Partner) -> str:
    keys = (
        await db.execute(
            select(ApiKey).where(ApiKey.partner_id == partner.id).order_by(ApiKey.created_at.desc())
        )
    ).scalars().all()
    return (
        _heading("ИНТЕГРАЦИЯ", "API-ключи",
                 "Ключи показаны только в маскированном виде. Секрет можно увидеть только при создании.",
                 _action("Управлять в Telegram", TG_LINK))
        + '<section class="panel table-panel">'
        + _table(["Название", "Префикс", "Создан", "Статус"],
                 [[_e(k.name), f'<code>{_e(k.key_prefix)}••••</code>',
                   _e(_when(k.created_at)), _pill("active" if k.is_active else "revoked")]
                  for k in keys],
                 "Активных или отозванных ключей пока нет.")
        + '</section><p class="context-note">Полные секреты не хранятся в открытом ви'
          'де и недоступны для повторного просмотра.</p>'
    )


async def _models(db: AsyncSession, partner: Partner) -> str:
    models = (
        await db.execute(select(Model).where(Model.status == "production").order_by(Model.name).limit(150))
    ).scalars().all()
    return (
        _heading("КАТАЛОГ", "Модели", "Публично доступные модели платформы и их назначения.",
                 _action("Смотреть цены", "/price"))
        + '<section class="panel table-panel">'
        + _table(["Модель", "ID модели", "Тип", "Статус"],
                 [[_e(m.name), f'<code>{_e(m.slug)}</code>', _e(m.modality), _pill("active")]
                  for m in models],
                 "Публичные модели ещё не настроены.")
        + '</section><p class="context-note">Индивидуальные тарифы и закрытые модели '
          'могут отличаться от публичного каталога. '
        'Фактическая цена фиксируется при принятии запроса.</p>'
    )


def _integration() -> str:
    example = (
        "curl https://api.нейроныч.online/v1/models \\\n"
        "  -H 'Authorization: Bearer YOUR_API_KEY'"
    )
    return (
        _heading("РАЗРАБОТЧИКАМ", "Подключение API",
                 "Единый API для текста, изображений и видео. Истина по параметрам — актуальная документация.",
                 _action("API Reference", "/guide"))
        + '<section class="panel integration-panel"><div class="step-num">01</div>'
        + '<div><h2>Получите API-ключ</h2><p>Откройте раздел «API-ключи», создайте кл'
          'юч в Telegram и сохраните секрет.</p></div></section>'
        + '<section class="panel integration-panel"><div class="step-num">02</div>'
        + '<div><h2>Выберите модель</h2><p>Проверьте доступность, режим и стоимость в каталоге.</p>'
        + '<a class="panel-link" href="/price">Актуальный прайс →</a></div></section>'
        + '<section class="panel integration-panel"><div class="step-num">03</div>'
        + '<div><h2>Отправьте первый запрос</h2><p>Используйте Bearer авторизацию и к'
          'орректный endpoint из документации.</p>'
        + f'<pre><code>{_e(example)}</code></pre></div></section>'
        + '<p class="context-note">Пример иллюстративный: перед интеграцией проверьте'
          ' endpoint и поддерживаемые параметры '
        'в API Reference. Не передавайте секреты в браузерный код или публичные репозитории.</p>'
    )


def _settings(partner: Partner) -> str:
    return (
        _heading("АККАУНТ", "Настройки", "Профиль партнёрского аккаунта. Для изменения данных обратитесь в поддержку.")
        + '<section class="panel account-panel">'
        + f'<div class="detail-row"><span>Компания</span><strong>{_e(partner.company_name)}</strong></div>'
        + f'<div class="detail-row"><span>Проект</span><strong>{_e(partner.project_name)}</strong></div>'
        + f'<div class="detail-row"><span>Telegram ID</span><strong><code>{_e(partner.telegram_id)}'
          f'</code></strong></div>'
        + f'<div class="detail-row"><span>Статус</span><strong>{_pill(partner.status)}</strong></div>'
        + f'<div class="detail-row"><span>Почта для чеков</span><strong>{_e(partner.payment_email or "Не указана")}'
          f'</strong></div>'
        + '</section><div class="panel help-panel"><div><h2>Нужна помощь?</h2>'
        + '<p>Вопросы по доступам, оплатам, лимитам и переносу аккаунта решаются через Telegram-поддержку.</p></div>'
        + _action("Написать", TG_LINK) + '</div>'
    )


async def render_cabinet(db: AsyncSession, partner: Partner, section: str) -> str:
    if section == "overview":
        body = await _overview(db, partner)
    elif section == "generations":
        body = await _generations(db, partner)
    elif section == "transactions":
        body = await _transactions(db, partner)
    elif section == "payments":
        body = await _payments(db, partner)
    elif section == "keys":
        body = await _keys(db, partner)
    elif section == "models":
        body = await _models(db, partner)
    elif section == "integration":
        body = _integration()
    elif section == "settings":
        body = _settings(partner)
    else:
        raise ValueError("unknown cabinet section")
    return _shell(partner, section, body)
