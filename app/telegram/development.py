"""Conversational development requests with durable, tenant-bound PostgreSQL storage.

The intake state is a BotDialog row locked by CabinetSessionMiddleware.
Every submission and notification is committed together; repeated callbacks
cannot create a second request after the state transitions to "menu".
"""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from app.infrastructure.config import get_settings
from app.payments.schemas import normalize_buyer_email
from app.telegram.models import DevelopmentInvoice, DevelopmentRequest
from app.telegram.service import is_admin, new_action, notify
from app.telegram.ui import format_msk, keyboard, show, status_label

FIELDS = ("kind", "platform", "goal", "integrations", "deadline", "budget", "contact")
QUESTIONS = (
    ("Какой бот вам нужен?", "Выберите направление. Если сомневаетесь, мы подскажем."),
    ("Где должен работать бот?", "Выберите основную платформу."),
    (
        "Что бот должен делать?",
        "Опишите задачу своими словами: кто будет пользоваться, что происходит на входе и какой результат нужен.\n"
        "Пример: «Принимать заявки из Telegram, уточнять детали и отправлять их менеджеру».\n"
        "От 15 до 1500 символов.",
    ),
    ("Нужны интеграции?", "Например, CRM, таблицы, платежи или сторонние API."),
    ("Когда хотелось бы запустить?", "Ориентировочный срок, не обязательство по разработке."),
    ("Какой планируете бюджет?", "Диапазоны помогают предложить реалистичный объём работ. Это не прайс."),
    (
        "Как с вами связаться?",
        "Можем ответить прямо в Telegram. Другой контакт указывать необязательно.\n"
        "Не присылайте пароли и API-ключи.",
    ),
)
CHOICES = {
    0: (
        ("⚙️ Автоматизация", "automation", "Автоматизация"),
        ("✨ Генеративный / AI-бот", "ai", "Генеративный / AI-бот"),
        ("🔗 Комбинированный", "hybrid", "Комбинированный"),
        ("🤔 Нужна консультация", "unsure", "Нужна консультация"),
    ),
    1: (
        ("Telegram", "telegram", "Telegram"),
        ("Сайт / веб-приложение", "web", "Сайт / веб-приложение"),
        ("WhatsApp", "whatsapp", "WhatsApp"),
        ("Несколько платформ", "multi", "Несколько платформ"),
        ("Пока не знаю", "unsure", "Платформа не определена"),
    ),
    3: (
        ("CRM / учёт заявок", "crm", "CRM / учёт заявок"),
        ("Таблицы и документы", "sheets", "Таблицы и документы"),
        ("Платежи и магазины", "payments", "Платежи и магазины"),
        ("Свои интеграции ✍️", "custom", None),
        ("Не нужны / не знаю", "none", "Пока не определены"),
    ),
    4: (
        ("Как можно быстрее", "urgent", "Как можно быстрее"),
        ("В течение месяца", "month", "В течение месяца"),
        ("1–3 месяца", "quarter", "1–3 месяца"),
        ("Срок не определён", "unsure", "Срок не определён"),
    ),
    5: (
        ("До 50 тыс. ₽", "under_50", "До 50 тыс. ₽"),
        ("50–150 тыс. ₽", "50_150", "50–150 тыс. ₽"),
        ("150–300 тыс. ₽", "150_300", "150–300 тыс. ₽"),
        ("Более 300 тыс. ₽", "over_300", "Более 300 тыс. ₽"),
        ("Бюджет пока не определён", "unsure", "Бюджет обсуждается"),
    ),
    6: (
        ("💬 Ответить мне в Telegram", "telegram", "Связаться в Telegram"),
        ("✍️ Укажу другой контакт", "custom", None),
    ),
}
SKIP = {
    3: "Пока не определены",
    4: "Срок не определён",
    5: "Бюджет обсуждается",
    6: "Связаться в Telegram",
}
LABELS = {
    "kind": "Тип",
    "platform": "Платформа",
    "goal": "Задача",
    "integrations": "Интеграции",
    "deadline": "Срок",
    "budget": "Бюджет",
    "contact": "Связь",
}
STATUSES = {
    "new": "Получена",
    "in_progress": "В работе",
    "completed": "Завершена",
}


def _telegram_id(event) -> str:
    return str(event.from_user.id)


def _answers(dialog) -> dict:
    return dict(dialog.data.get("answers") or {})


def _update(dialog, *, step: int, answers: dict, text_mode: bool = False) -> None:
    dialog.state = "development_intake"
    dialog.data = {"step": step, "answers": answers, "text_mode": text_mode}


def _summary(answers: dict) -> str:
    return "\n".join(f"{LABELS[name]}: {answers.get(name, '—')}" for name in FIELDS)


async def render(event, dialog, *, error: str = "") -> None:
    step = int(dialog.data.get("step", 0))
    answers = _answers(dialog)
    if step >= len(FIELDS):
        buttons = [
            ("✅ Отправить заявку", "dev:submit"),
            ("← Исправить ответы", "dev:back"),
            ("✖ Отменить", "dev:cancel"),
        ]
        policy = get_settings().privacy_policy_url
        if policy:
            buttons.insert(1, ("Политика конфиденциальности", policy))
        await show(
            event,
            "Проверьте заявку 🤖\n\n"
            + _summary(answers)
            + "\n\nПосле отправки заявку получит команда Нейроныча. "
            "Ответим по указанному контакту; это не заказ и не списание с баланса.\n"
            "Нажимая «Отправить заявку», вы соглашаетесь, что мы свяжемся с вами по вашему запросу.",
            keyboard(*buttons, back=None),
        )
        return

    title, hint = QUESTIONS[step]
    header = f"🤖 Заявка на разработку · шаг {step + 1} из {len(FIELDS)}\n"
    body = f"\n{title}\n\n{hint}"
    buttons = []
    if not dialog.data.get("text_mode"):
        buttons.extend((label, f"dev:choose:{code}") for label, code, _ in CHOICES.get(step, ()))
        if step in SKIP:
            buttons.append(("Пропустить", "dev:skip"))
    else:
        body += "\n\nОтправьте ответ одним сообщением."
    if step > 0:
        buttons.append(("← Назад", "dev:back"))
    buttons.append(("✖ Отмена", "dev:cancel"))
    await show(
        event,
        header + (f"\n{error}\n" if error else "") + body,
        keyboard(*buttons, back=None),
    )


async def _home(event, db, dialog) -> None:
    from app.telegram.handlers import home

    await home(event, db, dialog)


async def _save_answer(event, dialog, value: str) -> None:
    step = int(dialog.data["step"])
    answers = _answers(dialog)
    answers[FIELDS[step]] = value
    _update(dialog, step=step + 1, answers=answers)
    await render(event, dialog)


async def handle_message(event, db, dialog, value: str) -> None:
    step = int(dialog.data.get("step", 0))
    if step >= len(FIELDS):
        await render(event, dialog, error="Проверьте ответы и нажмите «Отправить заявку».")
        return
    if step != 2 and not dialog.data.get("text_mode"):
        await render(event, dialog, error="Выберите подходящий вариант кнопкой ниже.")
        return
    lower, upper = (15, 1500) if step == 2 else ((2, 300) if step == 3 else (3, 150))
    if not lower <= len(value) <= upper:
        await render(event, dialog, error=f"Введите от {lower} до {upper} символов. Ответ пока не сохранён.")
        return
    await _save_answer(event, dialog, value)


async def handle_callback(event, db, dialog, data: str) -> None:
    if data == "dev:start":
        _update(dialog, step=0, answers={})
        await render(event, dialog)
        return
    if data == "dev:my":
        await my_requests(event, db)
        return
    if data.startswith("dev:view:"):
        await view_request(event, db, data.rsplit(":", 1)[1])
        return
    if data == "dev:cancel":
        await _home(event, db, dialog)
        return
    if dialog.state != "development_intake":
        await show(
            event,
            "Эта анкета уже закрыта. Чтобы оставить новую заявку, начните заново.",
            keyboard(("🤖 Новая заявка", "dev:start"), ("Мои заявки", "dev:my"), back="main_menu"),
        )
        return
    step = int(dialog.data.get("step", 0))
    if data == "dev:back":
        if step == 0:
            await _home(event, db, dialog)
        else:
            _update(dialog, step=step - 1, answers=_answers(dialog))
            await render(event, dialog)
        return
    if data == "dev:submit":
        if step != len(FIELDS) or any(not _answers(dialog).get(field) for field in FIELDS):
            await render(event, dialog, error="Заполните все этапы перед отправкой.")
            return
        request = DevelopmentRequest(
            telegram_id=_telegram_id(event),
            telegram_username=(event.from_user.username or "").lower() or None,
            answers=_answers(dialog),
            status="new",
        )
        db.add(request)
        await db.flush()
        username = f"@{request.telegram_username}" if request.telegram_username else "без username"
        await notify(
            db,
            get_settings().admin_telegram_id,
            f"🤖 Новая заявка на разработку {request.id}\n"
            f"От: {username}, Telegram ID {request.telegram_id}\n\n"
            f"{_summary(request.answers)[:2900]}\n\n"
            "Откройте раздел «Заявки на разработку» в админке.",
            f"development-request:{request.id}",
        )
        dialog.state, dialog.data = "menu", {"last_development_request_id": request.id}
        # The acknowledgement cannot make a successfully submitted lead disappear.
        await db.commit()
        await show(
            event,
            f"✅ Заявка {request.id[:8]} отправлена.\n\n"
            "Мы получили описание проекта. Команда свяжется с вами для уточнения деталей, "
            "оценки стоимости и сроков. Баланс не изменился.",
            keyboard(("Мои заявки", "dev:my"), ("В главное меню", "main_menu"), back=None),
        )
        return
    if data == "dev:skip" and step in SKIP:
        await _save_answer(event, dialog, SKIP[step])
        return
    if data.startswith("dev:choose:") and step in CHOICES:
        code = data.rsplit(":", 1)[1]
        choice = next((option for option in CHOICES[step] if option[1] == code), None)
        if choice is None:
            await render(event, dialog, error="Эта кнопка устарела. Выберите вариант ниже.")
        elif choice[2] is None:
            _update(dialog, step=step, answers=_answers(dialog), text_mode=True)
            await render(event, dialog)
        else:
            await _save_answer(event, dialog, choice[2])
        return
    await render(event, dialog, error="Выберите действие на текущем шаге.")


async def my_requests(event, db) -> None:
    rows = (
        await db.execute(
            select(DevelopmentRequest)
            .where(DevelopmentRequest.telegram_id == _telegram_id(event))
            .order_by(DevelopmentRequest.created_at.desc(), DevelopmentRequest.id.desc())
            .limit(5)
        )
    ).scalars().all()
    buttons = [
        (f"{STATUSES.get(row.status, 'Рассматривается')} · {row.id[:8]}", f"dev:view:{row.id}")
        for row in rows
    ]
    buttons.append(("➕ Новая заявка", "dev:start"))
    status_list = "\n".join(
        f"• {row.id[:8]} · {STATUSES.get(row.status, 'Рассматривается')}" for row in rows
    )
    await show(
        event,
        ("Мои заявки на разработку\n\n" + status_list + "\n\nВыберите заявку для просмотра.")
        if rows
        else "Заявок пока нет.\n\nОпишите идею — мы поможем определить требования, сроки и бюджет.",
        keyboard(*buttons, back="main_menu"),
    )


async def view_request(event, db, request_id: str) -> None:
    row = await db.get(DevelopmentRequest, str(UUID(request_id)))
    if row is None or row.telegram_id != _telegram_id(event):
        raise HTTPException(404, "development_request_not_found")
    invoices = (
        await db.execute(
            select(DevelopmentInvoice)
            .where(DevelopmentInvoice.development_request_id == row.id)
            .order_by(DevelopmentInvoice.created_at)
        )
    ).scalars().all()
    payment_lines = []
    buttons = [("Мои заявки", "dev:my")]
    for invoice in invoices:
        label = "Аванс 30%" if invoice.installment == "advance" else "Остаток 70%"
        payment_lines.append(f"{label}: {invoice.requested_rub:.2f} ₽ · {status_label(invoice.status)}")
        if invoice.invoice_url and invoice.status != "paid":
            buttons.append((f"Оплатить · {label}", invoice.invoice_url))
    quote = f"\nСтоимость проекта: {row.quoted_price_rub:.2f} ₽\n" if row.quoted_price_rub else ""
    payments = "\n".join(payment_lines) or "Счета ещё не выставлены."
    await show(
        event,
        f"Заявка {row.id[:8]} · {STATUSES.get(row.status, 'Рассматривается')}\n"
        f"Создана: {format_msk(row.created_at)}\n{quote}\n{payments}\n\n{_summary(row.answers)}\n\n"
        "По этой заявке с вами свяжутся в Telegram или по указанному контакту.",
        keyboard(*buttons, back="main_menu"),
    )


async def admin_callback(event, db, dialog, data: str) -> None:
    if not is_admin(_telegram_id(event)):
        raise HTTPException(403, "admin_required")
    if data.startswith("admin_dev_quote:"):
        request_id = data.rsplit(":", 1)[1]
        row = await db.get(DevelopmentRequest, str(UUID(request_id)))
        if row is None:
            raise HTTPException(404, "development_request_not_found")
        has_invoice = await db.scalar(
            select(DevelopmentInvoice.id).where(DevelopmentInvoice.development_request_id == row.id).limit(1)
        )
        if has_invoice:
            raise HTTPException(409, "development_quote_locked")
        dialog.state = "admin_development_quote_price"
        dialog.data = {"request_id": row.id}
        await show(
            event,
            "Введите полную цену проекта целым числом в рублях.\n\n"
            "Клиент её не выбирает: цена задаётся администратором отдельно для этой заявки.",
            keyboard(back=f"admin_dev_view:{row.id}"),
        )
        return
    if data.startswith("admin_dev_invoice:"):
        _, request_id, installment = data.split(":")
        row = await db.get(DevelopmentRequest, str(UUID(request_id)))
        if row is None:
            raise HTTPException(404, "development_request_not_found")
        if row.quoted_price_rub is None or not row.buyer_email:
            raise HTTPException(409, "development_quote_required")
        from app.telegram.development_billing import installment_amount

        amount = installment_amount(row.quoted_price_rub, installment)
        label = "аванс 30%" if installment == "advance" else "остаток 70%"
        action = await new_action(
            db,
            _telegram_id(event),
            "admin_dev_invoice",
            {"request_id": row.id, "installment": installment},
        )
        await db.commit()
        await show(
            event,
            f"Выставить {label} на {amount:.2f} ₽?\n"
            f"Полная цена: {row.quoted_price_rub:.2f} ₽.\n"
            f"Email для чека: {row.buyer_email}\n\n"
            "После подтверждения клиент получит ссылку на оплату, а чек после оплаты — на email.",
            keyboard(("Подтвердить", f"confirm:{action.id}"), back=f"admin_dev_view:{row.id}"),
        )
        return
    if data.startswith("admin_dev_status:"):
        _, request_id, status = data.split(":")
        if status not in {"in_progress", "completed"}:
            raise HTTPException(422, "invalid_development_status")
        row = await db.get(DevelopmentRequest, str(UUID(request_id)), with_for_update=True)
        if row is None:
            raise HTTPException(404, "development_request_not_found")
        if row.status != status:
            row.status = status
            await notify(
                db,
                row.telegram_id,
                f"Статус вашей заявки {row.id[:8]} на разработку бота: {STATUSES[status]}.",
                f"development-status:{row.id}:{status}",
            )
        await db.commit()
        await admin_request(event, db, row.id)
        return
    if data.startswith("admin_dev_view:"):
        await admin_request(event, db, data.rsplit(":", 1)[1])
        return
    if data.startswith("admin_dev:"):
        raw_page = data.split(":", 1)[1]
        if not raw_page.isascii() or not raw_page.isdigit() or len(raw_page) > 6:
            raise HTTPException(422, "invalid_page")
        page = int(raw_page)
        rows = (
            await db.execute(
                select(DevelopmentRequest)
                .order_by(DevelopmentRequest.created_at.desc(), DevelopmentRequest.id.desc())
                .offset(page * 5)
                .limit(6)
            )
        ).scalars().all()
        buttons = [
            (
                f"{STATUSES.get(row.status, '?')} · {row.answers.get('kind', 'Бот')[:25]} · {row.id[:8]}",
                f"admin_dev_view:{row.id}",
            )
            for row in rows[:5]
        ]
        if page:
            buttons.append(("← Новее", f"admin_dev:{page - 1}"))
        if len(rows) > 5:
            buttons.append(("Старее →", f"admin_dev:{page + 1}"))
        await show(
            event,
            "Заявки на разработку ботов\n\n" + ("Выберите заявку для обработки." if rows else "Заявок пока нет."),
            keyboard(*buttons, back="admin_menu"),
        )
        return
    raise HTTPException(404, "development_request_not_found")


async def admin_request(event, db, request_id: str) -> None:
    if not is_admin(_telegram_id(event)):
        raise HTTPException(403, "admin_required")
    row = await db.get(DevelopmentRequest, str(UUID(request_id)))
    if row is None:
        raise HTTPException(404, "development_request_not_found")
    username = f"@{row.telegram_username}" if row.telegram_username else "не указан"
    invoices = (
        await db.execute(
            select(DevelopmentInvoice)
            .where(DevelopmentInvoice.development_request_id == row.id)
            .order_by(DevelopmentInvoice.created_at)
        )
    ).scalars().all()
    by_kind = {invoice.installment: invoice for invoice in invoices}
    buttons = []
    if row.status == "new":
        buttons.append(("▶️ Взять в работу", f"admin_dev_status:{row.id}:in_progress"))
    if row.status != "completed":
        buttons.append(("✅ Завершить", f"admin_dev_status:{row.id}:completed"))
    if not invoices:
        buttons.append(("💰 Указать цену и email", f"admin_dev_quote:{row.id}"))
    if row.quoted_price_rub is not None and "advance" not in by_kind:
        buttons.append(("🧾 Выставить аванс 30%", f"admin_dev_invoice:{row.id}:advance"))
    advance = by_kind.get("advance")
    if advance is not None and advance.settled_at is not None and "final" not in by_kind:
        buttons.append(("🧾 Выставить остаток 70%", f"admin_dev_invoice:{row.id}:final"))
    for invoice in invoices:
        label = "Аванс 30%" if invoice.installment == "advance" else "Остаток 70%"
        if invoice.invoice_url and invoice.status != "paid":
            buttons.append((f"Ссылка · {label}", invoice.invoice_url))
    quote = (
        f"\nЦена проекта: {row.quoted_price_rub:.2f} ₽\nEmail для чека: {row.buyer_email}"
        if row.quoted_price_rub is not None
        else "\nЦена проекта: не назначена"
    )
    payments = "\n".join(
        f"{'Аванс 30%' if invoice.installment == 'advance' else 'Остаток 70%'}: "
        f"{invoice.requested_rub:.2f} ₽ · {status_label(invoice.status)}"
        for invoice in invoices
    ) or "Счета: не выставлены"
    await show(
        event,
        f"Заявка {row.id}\n"
        f"Статус: {STATUSES.get(row.status, 'Рассматривается')}\n"
        f"Создана: {format_msk(row.created_at)}\n"
        f"Telegram: {username} · ID {row.telegram_id}{quote}\n{payments}\n\n{_summary(row.answers)}",
        keyboard(*buttons, back="admin_dev:0"),
    )


async def admin_quote_message(event, db, dialog, value: str) -> None:
    if not is_admin(_telegram_id(event)):
        raise HTTPException(403, "admin_required")
    if dialog.state == "admin_development_quote_price":
        if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= 9999999999999999:
            raise HTTPException(422, "invalid_development_price")
        dialog.state = "admin_development_quote_email"
        dialog.data = {**dialog.data, "price": value}
        await show(
            event,
            "Введите email заказчика для отдельного чека по разработке.",
            keyboard(back=f"admin_dev_view:{dialog.data['request_id']}"),
        )
        return
    if dialog.state != "admin_development_quote_email":
        raise HTTPException(409, "development_quote_not_started")
    email = normalize_buyer_email(value)
    request_id = str(UUID(dialog.data["request_id"]))
    from app.telegram.development_billing import installment_amount

    price = int(dialog.data["price"])
    advance = installment_amount(price, "advance")
    final = installment_amount(price, "final")
    action = await new_action(
        db,
        _telegram_id(event),
        "admin_dev_quote",
        {"request_id": request_id, "price": str(price), "buyer_email": email},
    )
    dialog.state, dialog.data = "menu", {}
    await db.commit()
    await show(
        event,
        f"Сохранить цену проекта {price:.2f} ₽?\n"
        f"Аванс 30%: {advance:.2f} ₽. Остаток 70%: {final:.2f} ₽.\n"
        f"Email для чека: {email}",
        keyboard(("Подтвердить", f"confirm:{action.id}"), back=f"admin_dev_view:{request_id}"),
    )
