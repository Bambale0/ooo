"""Admin-only recipient selection. Money changes remain in confirmed admin forms."""

import re
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import Select, select

from app.accounts.models import Partner
from app.telegram.models import BotDialog
from app.telegram.service import is_admin
from app.telegram.ui import keyboard, show

PAGE_SIZE = 4
SEARCH_HINT = "Выберите партнёра кнопкой или отправьте Telegram ID / @username."


def require_picker(event, dialog) -> None:
    if not is_admin(str(event.from_user.id)):
        raise HTTPException(403, "admin_required")
    if dialog.state != "admin_partner_pick" or dialog.data.get("form") != "adjustment":
        raise HTTPException(409, "confirmation_expired")


def partner_query() -> Select[tuple[Partner, str | None]]:
    return select(Partner, BotDialog.telegram_username).outerjoin(
        BotDialog, BotDialog.telegram_id == Partner.telegram_id
    )


def recipient_label(partner: Partner, username: str | None) -> str:
    username_label = f" · @{username}" if username else ""
    return f"{partner.telegram_id} · {partner.company_name[:25]}{username_label}"


async def recipient_details(db, partner_id: str) -> str:
    row = (await db.execute(partner_query().where(Partner.id == partner_id))).first()
    if row is None:
        raise HTTPException(404, "partner_not_found")
    partner, username = row
    status = {"active": "активен", "disabled": "отключён", "deleted": "удалён"}.get(partner.status, partner.status)
    return (
        f"Партнёр: {partner.company_name}\nПроект: {partner.project_name}\n"
        f"Telegram ID: {partner.telegram_id}\n"
        f"Последний известный username: {('@' + username) if username else 'не сохранён'}\n"
        f"Статус: {status}\nБаланс: {partner.balance_rub:.2f} ₽\nПроверьте Telegram ID получателя."
    )


async def start_picker(event, db, dialog) -> None:
    if not is_admin(str(event.from_user.id)):
        raise HTTPException(403, "admin_required")
    dialog.state, dialog.data = "admin_partner_pick", {"form": "adjustment", "search": None}
    await show_page(event, db, dialog)


async def show_page(event, db, dialog, page: int = 0) -> None:
    require_picker(event, dialog)
    query = partner_query()
    search = dialog.data.get("search")
    if search:
        field = {
            "telegram_id": Partner.telegram_id,
            "telegram_username": BotDialog.telegram_username,
            "id": Partner.id,
        }[search["field"]]
        query = query.where(field == search["value"])
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


async def choose_partner(event, db, dialog, partner_id: str) -> None:
    require_picker(event, dialog)
    details = await recipient_details(db, str(UUID(partner_id)))
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
        keyboard(("Другой партнёр", "admin_form:adjustment"), back="admin_ops"),
    )


async def search_partners(event, db, dialog, value: str) -> None:
    require_picker(event, dialog)
    value = value.strip()
    if value.isascii() and value.isdigit() and 1 <= len(value) <= 20:
        search = {"field": "telegram_id", "value": str(int(value))}
    elif re.fullmatch(r"@?[A-Za-z0-9_]{1,32}", value):
        search = {"field": "telegram_username", "value": value.lstrip("@").lower()}
    else:
        try:
            search = {"field": "id", "value": str(UUID(value))}
        except ValueError:
            await show(event, "Введите числовой Telegram ID или @username.", keyboard(back="admin_form:adjustment"))
            return
    dialog.data = {**dialog.data, "search": search}
    field = {"telegram_id": Partner.telegram_id, "telegram_username": BotDialog.telegram_username, "id": Partner.id}[
        search["field"]
    ]
    rows = (await db.execute(partner_query().where(field == search["value"]).limit(2))).all()
    if len(rows) == 1 and search["field"] != "telegram_username":
        await choose_partner(event, db, dialog, rows[0][0].id)
    else:
        await show_page(event, db, dialog)


async def handle_picker_callback(event, db, dialog, data: str) -> None:
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
