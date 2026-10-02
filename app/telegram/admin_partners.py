"""Admin partner browser and recipient selection for confirmed operations."""

import re
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import Select, func, or_, select

from app.accounts.models import ApiKey, Partner
from app.billing.models import LedgerEntry
from app.catalog.models import Model, PartnerModelGrant
from app.generations.models import Generation
from app.payments.models import PaymentInvoice
from app.providers.models import ProviderAttempt, ProviderCredential
from app.telegram.models import BotDialog
from app.telegram.service import is_admin
from app.telegram.ui import keyboard, show

PAGE_SIZE = 4
DETAIL_PAGE_SIZE = 5
SEARCH_HINT = "Выберите партнёра кнопкой или отправьте Telegram ID / @username."


def require_admin(event) -> None:
    if not is_admin(str(event.from_user.id)):
        raise HTTPException(403, "admin_required")


def require_picker(event, dialog) -> None:
    require_admin(event)
    if dialog.state != "admin_partner_pick" or dialog.data.get("form") != "adjustment":
        raise HTTPException(409, "confirmation_expired")


def partner_query() -> Select[tuple[Partner, str | None]]:
    return select(Partner, BotDialog.telegram_username).outerjoin(
        BotDialog, BotDialog.telegram_id == Partner.telegram_id
    )


def recipient_label(partner: Partner, username: str | None) -> str:
    username_label = f" · @{username}" if username else ""
    return f"{partner.telegram_id} · {partner.company_name[:25]}{username_label}"


def admin_partner_label(partner: Partner, username: str | None) -> str:
    status_label = {"active": "✓", "disabled": "⏸", "deleted": "×"}.get(partner.status, "•")
    return f"{status_label} {recipient_label(partner, username)}"


def _status(partner: Partner) -> str:
    return {"active": "активен", "disabled": "отключён", "deleted": "удалён"}.get(partner.status, partner.status)


def _date(value) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%d.%m.%Y %H:%M")
    return str(value or "—")


def _search_filter(search: dict | None):
    if not search:
        return None
    field, value = search["field"], search["value"]
    if field == "telegram_id":
        return Partner.telegram_id == value
    if field == "telegram_username":
        return BotDialog.telegram_username == value
    if field == "id":
        return Partner.id == value
    if field == "text":
        pattern = f"%{value}%"
        return or_(Partner.company_name.ilike(pattern), Partner.project_name.ilike(pattern))
    raise HTTPException(400, "invalid_partner_search")


def _parse_picker_search(value: str) -> dict:
    value = value.strip()
    if value.isascii() and value.isdigit() and 1 <= len(value) <= 20:
        return {"field": "telegram_id", "value": str(int(value))}
    if re.fullmatch(r"@?[A-Za-z0-9_]{1,32}", value):
        return {"field": "telegram_username", "value": value.lstrip("@").lower()}
    try:
        return {"field": "id", "value": str(UUID(value))}
    except ValueError as exc:
        raise ValueError("invalid_partner_search") from exc


def _parse_admin_search(value: str) -> dict:
    value = value.strip()
    if value.isascii() and value.isdigit() and 1 <= len(value) <= 20:
        return {"field": "telegram_id", "value": str(int(value))}
    if value.startswith("@") and re.fullmatch(r"@[A-Za-z0-9_]{1,32}", value):
        return {"field": "telegram_username", "value": value[1:].lower()}
    try:
        return {"field": "id", "value": str(UUID(value))}
    except ValueError:
        pass
    if 2 <= len(value) <= 100:
        return {"field": "text", "value": value}
    raise ValueError("invalid_partner_search")


async def _partner_row(db, partner_id: str) -> tuple[Partner, str | None]:
    row = (await db.execute(partner_query().where(Partner.id == str(UUID(partner_id))))).first()
    if row is None:
        raise HTTPException(404, "partner_not_found")
    return row[0], row[1]


async def recipient_details(db, partner_id: str) -> str:
    partner, username = await _partner_row(db, partner_id)
    return (
        f"Партнёр: {partner.company_name}\nПроект: {partner.project_name}\n"
        f"Telegram ID: {partner.telegram_id}\n"
        f"Последний известный username: {('@' + username) if username else 'не сохранён'}\n"
        f"Статус: {_status(partner)}\nБаланс: {partner.balance_rub:.2f} ₽\nПроверьте Telegram ID получателя."
    )


async def start_picker(event, db, dialog) -> None:
    require_admin(event)
    dialog.state, dialog.data = "admin_partner_pick", {"form": "adjustment", "search": None}
    await show_page(event, db, dialog)


async def show_page(event, db, dialog, page: int = 0) -> None:
    require_picker(event, dialog)
    query = partner_query()
    search = dialog.data.get("search")
    condition = _search_filter(search)
    if condition is not None:
        query = query.where(condition)
    rows = (
        await db.execute(query.order_by(Partner.created_at, Partner.id).offset(page * PAGE_SIZE).limit(PAGE_SIZE + 1))
    ).all()
    buttons = [
        (recipient_label(partner, username), f"admin_partner_pick:{partner.id}")
        for partner, username in rows[:PAGE_SIZE]
    ]
    if page:
        buttons.append(("← Предыдущие", f"admin_partner_page:{page - 1}"))
    if len(rows) > PAGE_SIZE:
        buttons.append(("Следующие →", f"admin_partner_page:{page + 1}"))
    buttons.append(("Поиск по Telegram ID / @username", "admin_partner_search"))
    if search:
        buttons.append(("Все партнёры", "admin_partner_list"))
    if rows:
        title = "Найденные партнёры" if search else f"Партнёры · страница {page + 1}"
        text = f"{title}\n{SEARCH_HINT}"
        if search and search["field"] == "telegram_username":
            text += "\nUsername сохранён при последнем обращении. Проверьте Telegram ID получателя."
    else:
        text = (
            "Партнёр не найден. Проверьте Telegram ID или откройте весь список. "
            "Username появляется после обращения партнёра к боту."
            if search
            else "Партнёров на этой странице нет."
        )
    await show(event, text, keyboard(*buttons, back="admin_ops"))


async def begin_adjustment(event, db, dialog, partner_id: str) -> None:
    require_admin(event)
    details = await recipient_details(db, partner_id)
    dialog.state, dialog.data = (
        "admin_form_input",
        {
            "form": "adjustment",
            "values": {"partner_id": str(UUID(partner_id))},
            "index": 1,
        },
    )
    await show(
        event,
        f"{details}\n\nВведите сумму RUB со знаком, например +500 или -100.",
        keyboard(("Другой партнёр", "admin_form:adjustment"), back=f"admin_partner_view:{partner_id}"),
    )


async def choose_partner(event, db, dialog, partner_id: str) -> None:
    require_picker(event, dialog)
    await begin_adjustment(event, db, dialog, partner_id)


async def search_partners(event, db, dialog, value: str) -> None:
    require_picker(event, dialog)
    try:
        search = _parse_picker_search(value)
    except ValueError:
        await show(
            event,
            "Введите числовой Telegram ID, @username, UUID или название.",
            keyboard(back="admin_form:adjustment"),
        )
        return
    dialog.data = {**dialog.data, "search": search}
    query = partner_query()
    condition = _search_filter(search)
    rows = (await db.execute(query.where(condition).limit(2))).all()
    if len(rows) == 1 and search["field"] != "telegram_username":
        await choose_partner(event, db, dialog, rows[0][0].id)
    else:
        await show_page(event, db, dialog)


async def show_admin_partners(event, db, dialog, page: int = 0, *, search: dict | None = None) -> None:
    require_admin(event)
    if search is None and dialog.state == "admin_partner_browse":
        search = dialog.data.get("search")
    query = partner_query()
    condition = _search_filter(search)
    if condition is not None:
        query = query.where(condition)
    rows = (
        await db.execute(
            query.order_by(Partner.created_at.desc(), Partner.id)
            .offset(page * PAGE_SIZE)
            .limit(PAGE_SIZE + 1)
        )
    ).all()
    dialog.state, dialog.data = "admin_partner_browse", {"search": search}
    buttons = [
        (admin_partner_label(partner, username), f"admin_partner_view:{partner.id}")
        for partner, username in rows[:PAGE_SIZE]
    ]
    if page:
        buttons.append(("← Предыдущие", f"admin_partners:{page - 1}"))
    if len(rows) > PAGE_SIZE:
        buttons.append(("Следующие →", f"admin_partners:{page + 1}"))
    buttons.append(("Поиск", "admin_partners_search"))
    if search:
        buttons.append(("Сбросить поиск", "admin_partners_all"))
    title = f"Партнёры · страница {page + 1}" if rows else "Партнёров не найдено."
    hint = "\nПоиск: Telegram ID, @username, UUID, компания или проект." if rows else ""
    await show(event, title + hint, keyboard(*buttons, back="admin_menu"))


async def prompt_admin_partner_search(event, dialog) -> None:
    require_admin(event)
    dialog.state = "admin_partner_browse_search"
    dialog.data = {}
    await show(
        event,
        "Введите Telegram ID, @username, UUID, название компании или проекта.",
        keyboard(back="admin_partners:0"),
    )


async def search_admin_partners(event, db, dialog, value: str) -> None:
    require_admin(event)
    try:
        search = _parse_admin_search(value)
    except ValueError:
        await show(
            event,
            "Введите Telegram ID, @username, UUID или не менее 2 символов названия.",
            keyboard(back="admin_partners:0"),
        )
        return
    query = partner_query()
    rows = (await db.execute(query.where(_search_filter(search)).limit(2))).all()
    if len(rows) == 1 and search["field"] != "telegram_username":
        await show_partner_card(event, db, dialog, rows[0][0].id)
        return
    await show_admin_partners(event, db, dialog, search=search)


async def _count(db, model, partner_id: str) -> int:
    return int(
        await db.scalar(select(func.count()).select_from(model).where(model.partner_id == partner_id))
        or 0
    )


async def show_partner_card(event, db, dialog, partner_id: str) -> None:
    require_admin(event)
    partner, username = await _partner_row(db, partner_id)
    counts = {
        "keys": await _count(db, ApiKey, partner.id),
        "credentials": await _count(db, ProviderCredential, partner.id),
        "payments": await _count(db, PaymentInvoice, partner.id),
        "generations": await _count(db, Generation, partner.id),
        "ledger": await _count(db, LedgerEntry, partner.id),
        "models": await _count(db, PartnerModelGrant, partner.id),
    }
    dialog.state, dialog.data = "admin_partner_browse", {"partner_id": partner.id}
    text = (
        f"{partner.company_name}\n"
        f"Проект: {partner.project_name}\n"
        f"UUID: {partner.id}\n"
        f"Telegram: {partner.telegram_id}"
        f"{(' · @' + username) if username else ''}\n"
        f"Статус: {_status(partner)}\n"
        f"Баланс: {partner.balance_rub:.2f} ₽\n"
        f"Покрытие себестоимости: {partner.cost_coverage_rub:.2f} ₽\n"
        f"Создан: {_date(partner.created_at)}\n"
        f"Заявка: {partner.application_id or '—'}\n\n"
        f"API-ключей: {counts['keys']} · ключей поставщика: {counts['credentials']}\n"
        f"Платежей: {counts['payments']} · генераций: {counts['generations']}\n"
        f"Операций ledger: {counts['ledger']} · доступов к моделям: {counts['models']}"
    )
    buttons = [
        ("API-ключи", f"admin_partner_keys:{partner.id}"),
        ("Ключи поставщика", f"admin_partner_creds:{partner.id}"),
        ("Платежи", f"admin_partner_payments:{partner.id}"),
        ("Генерации", f"admin_partner_gens:{partner.id}"),
        ("Ledger", f"admin_partner_ledger:{partner.id}"),
        ("Доступ к моделям", f"admin_partner_models:{partner.id}"),
    ]
    if partner.status != "deleted":
        buttons.extend(
            [
                ("Корректировать баланс", f"admin_partner_adjust:{partner.id}"),
                (
                    "Отключить" if partner.status == "active" else "Включить",
                    f"admin_{'disable' if partner.status == 'active' else 'enable'}:{partner.id}",
                ),
                ("Перенести Telegram ID", f"admin_transfer:{partner.id}"),
            ]
        )
    await show(event, text, keyboard(*buttons, back="admin_partners:0"))


def _section_navigation(prefix: str, partner_id: str, page: int, has_more: bool):
    buttons = []
    if page:
        buttons.append(("← Предыдущие", f"{prefix}:{partner_id}:{page - 1}"))
    if has_more:
        buttons.append(("Следующие →", f"{prefix}:{partner_id}:{page + 1}"))
    return buttons


def _parse_section(data: str) -> tuple[str, int]:
    parts = data.split(":")
    partner_id = str(UUID(parts[1]))
    page = int(parts[2]) if len(parts) > 2 else 0
    if page < 0 or page > 100000:
        raise HTTPException(400, "invalid_page")
    return partner_id, page


async def show_partner_keys(event, db, data: str) -> None:
    require_admin(event)
    partner_id, page = _parse_section(data)
    await _partner_row(db, partner_id)
    rows = (
        await db.execute(
            select(ApiKey)
            .where(ApiKey.partner_id == partner_id)
            .order_by(ApiKey.created_at.desc())
            .offset(page * DETAIL_PAGE_SIZE)
            .limit(DETAIL_PAGE_SIZE + 1)
        )
    ).scalars().all()
    lines = ["API-ключи партнёра"]
    for row in rows[:DETAIL_PAGE_SIZE]:
        state = "активен" if row.is_active else "отозван"
        webhook = "webhook: да" if row.webhook_url else "webhook: нет"
        lines.append(f"{row.name} · {row.key_prefix}… · {state} · {webhook}\n{_date(row.created_at)}")
    if len(lines) == 1:
        lines.append("Ключей нет.")
    buttons = _section_navigation("admin_partner_keys", partner_id, page, len(rows) > DETAIL_PAGE_SIZE)
    await show(event, "\n\n".join(lines), keyboard(*buttons, back=f"admin_partner_view:{partner_id}"))


async def show_partner_credentials(event, db, data: str) -> None:
    require_admin(event)
    partner_id, page = _parse_section(data)
    await _partner_row(db, partner_id)
    rows = (
        await db.execute(
            select(ProviderCredential)
            .where(ProviderCredential.partner_id == partner_id)
            .order_by(ProviderCredential.created_at.desc())
            .offset(page * DETAIL_PAGE_SIZE)
            .limit(DETAIL_PAGE_SIZE + 1)
        )
    ).scalars().all()
    lines = ["Ключи поставщика"]
    for row in rows[:DETAIL_PAGE_SIZE]:
        state = "активен" if row.is_active else "отозван"
        lines.append(f"{row.provider} · {row.label} · {row.key_prefix}… · {state}\n{_date(row.created_at)}")
    if len(lines) == 1:
        lines.append("Ключей поставщика нет.")
    buttons = _section_navigation("admin_partner_creds", partner_id, page, len(rows) > DETAIL_PAGE_SIZE)
    await show(event, "\n\n".join(lines), keyboard(*buttons, back=f"admin_partner_view:{partner_id}"))


async def show_partner_payments(event, db, data: str) -> None:
    require_admin(event)
    partner_id, page = _parse_section(data)
    await _partner_row(db, partner_id)
    rows = (
        await db.execute(
            select(PaymentInvoice)
            .where(PaymentInvoice.partner_id == partner_id)
            .order_by(PaymentInvoice.created_at.desc())
            .offset(page * DETAIL_PAGE_SIZE)
            .limit(DETAIL_PAGE_SIZE + 1)
        )
    ).scalars().all()
    lines = ["Платежи партнёра"]
    for row in rows[:DETAIL_PAGE_SIZE]:
        lines.append(
            f"UUID: {row.id}\n"
            f"{row.requested_rub:.2f} ₽ · {row.status} · возврат {row.refunded_rub:.2f} ₽\n"
            f"{_date(row.created_at)}"
        )
    if len(lines) == 1:
        lines.append("Платежей нет.")
    buttons = _section_navigation("admin_partner_payments", partner_id, page, len(rows) > DETAIL_PAGE_SIZE)
    await show(event, "\n\n".join(lines), keyboard(*buttons, back=f"admin_partner_view:{partner_id}"))


async def show_partner_generations(event, db, data: str) -> None:
    require_admin(event)
    partner_id, page = _parse_section(data)
    await _partner_row(db, partner_id)
    rows = (
        await db.execute(
            select(Generation)
            .where(Generation.partner_id == partner_id)
            .order_by(Generation.created_at.desc())
            .offset(page * DETAIL_PAGE_SIZE)
            .limit(DETAIL_PAGE_SIZE + 1)
        )
    ).scalars().all()
    visible = rows[:DETAIL_PAGE_SIZE]
    generation_ids = [row.id for row in visible]
    attempts = (
        list(
            (
                await db.execute(
                    select(ProviderAttempt).where(ProviderAttempt.generation_id.in_(generation_ids))
                )
            ).scalars()
        )
        if generation_ids
        else []
    )
    attempts_by_generation = {attempt.generation_id: attempt for attempt in attempts}
    lines = ["Генерации партнёра"]
    for row in visible:
        charge = row.actual_charge_rub if row.actual_charge_rub is not None else row.partner_price_rub
        attempt = attempts_by_generation.get(row.id)
        attempt_line = f"\nAttempt UUID: {attempt.id}" if attempt is not None else ""
        lines.append(
            f"Generation UUID: {row.id}{attempt_line}\n"
            f"{row.model_slug} · {row.mode}/{row.resolution}\n"
            f"{row.status} · {charge:.2f} ₽ · {_date(row.created_at)}"
        )
    if len(lines) == 1:
        lines.append("Генераций нет.")
    buttons = _section_navigation("admin_partner_gens", partner_id, page, len(rows) > DETAIL_PAGE_SIZE)
    await show(event, "\n\n".join(lines), keyboard(*buttons, back=f"admin_partner_view:{partner_id}"))


async def show_partner_ledger(event, db, data: str) -> None:
    require_admin(event)
    partner_id, page = _parse_section(data)
    await _partner_row(db, partner_id)
    rows = (
        await db.execute(
            select(LedgerEntry)
            .where(LedgerEntry.partner_id == partner_id)
            .order_by(LedgerEntry.created_at.desc())
            .offset(page * DETAIL_PAGE_SIZE)
            .limit(DETAIL_PAGE_SIZE + 1)
        )
    ).scalars().all()
    lines = ["Ledger партнёра"]
    for row in rows[:DETAIL_PAGE_SIZE]:
        lines.append(
            f"{row.operation_type} · {row.amount_rub:+.2f} ₽ → {row.balance_after_rub:.2f} ₽\n"
            f"{(row.description or 'без описания')[:180]} · {_date(row.created_at)}"
        )
    if len(lines) == 1:
        lines.append("Операций ledger нет.")
    buttons = _section_navigation("admin_partner_ledger", partner_id, page, len(rows) > DETAIL_PAGE_SIZE)
    await show(event, "\n\n".join(lines), keyboard(*buttons, back=f"admin_partner_view:{partner_id}"))


async def show_partner_models(event, db, data: str) -> None:
    require_admin(event)
    partner_id, page = _parse_section(data)
    await _partner_row(db, partner_id)
    rows = (
        await db.execute(
            select(PartnerModelGrant, Model)
            .join(Model, Model.id == PartnerModelGrant.model_id)
            .where(PartnerModelGrant.partner_id == partner_id)
            .order_by(PartnerModelGrant.created_at.desc())
            .offset(page * DETAIL_PAGE_SIZE)
            .limit(DETAIL_PAGE_SIZE + 1)
        )
    ).all()
    lines = ["Доступ к restricted-моделям"]
    for grant, model in rows[:DETAIL_PAGE_SIZE]:
        state = "активен" if grant.revoked_at is None else f"отозван {_date(grant.revoked_at)}"
        lines.append(f"{model.slug} · {state}\n{grant.reason[:180]} · {_date(grant.created_at)}")
    if len(lines) == 1:
        lines.append("Индивидуальных доступов нет.")
    buttons = _section_navigation("admin_partner_models", partner_id, page, len(rows) > DETAIL_PAGE_SIZE)
    await show(event, "\n\n".join(lines), keyboard(*buttons, back=f"admin_partner_view:{partner_id}"))


async def handle_picker_callback(event, db, dialog, data: str) -> None:
    require_admin(event)
    if data.startswith("admin_partner_view:"):
        await show_partner_card(event, db, dialog, data.split(":", 1)[1])
    elif data.startswith("admin_partner_adjust:"):
        await begin_adjustment(event, db, dialog, data.split(":", 1)[1])
    elif data.startswith("admin_partner_keys:"):
        await show_partner_keys(event, db, data)
    elif data.startswith("admin_partner_creds:"):
        await show_partner_credentials(event, db, data)
    elif data.startswith("admin_partner_payments:"):
        await show_partner_payments(event, db, data)
    elif data.startswith("admin_partner_gens:"):
        await show_partner_generations(event, db, data)
    elif data.startswith("admin_partner_ledger:"):
        await show_partner_ledger(event, db, data)
    elif data.startswith("admin_partner_models:"):
        await show_partner_models(event, db, data)
    else:
        require_picker(event, dialog)
        if data == "admin_partner_list":
            dialog.data = {**dialog.data, "search": None}
            await show_page(event, db, dialog)
        elif data == "admin_partner_search":
            await show(event, "Отправьте Telegram ID или @username партнёра.", keyboard(back="admin_partner_list"))
        elif data.startswith("admin_partner_page:"):
            from app.telegram.handlers import page_number

            await show_page(event, db, dialog, page_number(data))
        elif data.startswith("admin_partner_pick:"):
            await choose_partner(event, db, dialog, data.split(":", 1)[1])
