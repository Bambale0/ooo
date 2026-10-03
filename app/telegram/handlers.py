"""Cabinet navigation and conversational input. No raw SQL/user markup."""

from uuid import UUID

from aiogram.types import FSInputFile
from fastapi import HTTPException
from sqlalchemy import select

from app.accounts.models import ApiKey, PartnerApplication
from app.accounts.router import submit_application, update_partner_api_key_webhook
from app.accounts.schemas import ApiKeyWebhookUpdate, PartnerApplicationCreate
from app.billing.models import LedgerEntry
from app.billing.safe_to_withdraw import calculate_safe_to_withdraw
from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.payments.crypto_pay import get_crypto_pay_client
from app.payments.models import PaymentInvoice
from app.payments.service import apply_paid_provider_invoice, create_or_resume_invoice, get_partner_payment
from app.support.models import SupportAttachment, SupportTicket
from app.support.service import append_message, attachment_for, get_ticket, ticket_messages
from app.telegram.actions import confirm_action
from app.telegram.service import is_admin, new_action, notify, partner_for
from app.telegram.ui import keyboard, show, status_label


def actor(event) -> str:
    return str(event.from_user.id)


def reset(dialog) -> None:
    dialog.state, dialog.data = "menu", {}


async def ask_confirmation(event, db, kind: str, payload: dict, label: str) -> None:
    action = await new_action(db, actor(event), kind, payload)
    await db.commit()
    await show(event, label, keyboard(("Подтвердить", f"confirm:{action.id}")))


async def registration_documents(event, dialog) -> None:
    settings = get_settings()
    reset(dialog)
    buttons = []
    if is_admin(actor(event)):
        buttons.append(("Администратор", "admin_menu"))
    if not settings.terms_url or not settings.privacy_policy_url:
        await show(
            event,
            "Добро пожаловать в Нейроныч!\n\n"
            "Мы готовим документы для подключения партнёров. Приём заявок откроется, "
            "когда здесь появятся условия и политика конфиденциальности. "
            "Пожалуйста, загляните позже — команда /start обновит этот экран.",
            keyboard(*buttons, back=None),
        )
        return
    dialog.state, dialog.data = "consent", {"legal_version": settings.legal_document_version}
    await show(
        event,
        "Добро пожаловать в Нейроныч! 👋\n\n"
        "Здесь можно подключить API нейросетей к вашему проекту, управлять ключами, "
        "пополнять баланс и обращаться в поддержку.\n\n"
        "Перед подачей заявки, пожалуйста, откройте и прочитайте оба документа: "
        "условия использования и политику конфиденциальности. "
        f"Версия документов: {settings.legal_document_version}.\n\n"
        "Нажимая «Принимаю оба документа», вы подтверждаете согласие с этой версией. "
        "Затем попросим название компании и краткое описание проекта — это два шага анкеты. "
        "После второго шага заявка отправится на рассмотрение. Решение сообщим в этом чате.",
        keyboard(
            ("Условия использования", settings.terms_url),
            ("Политика конфиденциальности", settings.privacy_policy_url),
            ("Принимаю оба документа", "accept_legal"),
            *buttons, back=None,
        ),
    )


async def home(event, db, dialog) -> None:
    if dialog.state == "admin_application_key":
        from app.telegram.admin_applications import cancel_approvals

        await cancel_approvals(db, actor(event), dialog.data["application_id"])
    reset(dialog)
    partner = await partner_for(db, actor(event))
    if partner:
        buttons = [
            ("Бесплатные видеотесты", "trials"),
            ("Документы", "legal_documents"),
            ("Баланс и пополнение", "balance"),
            ("API-ключи", "api_keys:0"),
            ("История и поиск", "history:0"),
            ("Поддержка", "support"),
            ("Документация и аккаунт", "settings"),
        ]
        if is_admin(actor(event)):
            buttons.append(("Администратор", "admin_menu"))
        await show(
            event,
            f"Рады видеть вас в кабинете Нейроныча! 👋\nБаланс: {partner.balance_rub:.2f} ₽\n\n"
            "Для первого знакомства выберите «Бесплатные видеотесты». "
            "Для подключения своего приложения создайте API-ключ и откройте документацию.\n\n"
            "В разделе баланса можно пополнить счёт и проверить оплату, в истории — найти операцию. "
            "Если понадобится помощь, напишите в поддержку. Команда /start всегда возвращает сюда.",
            keyboard(*buttons, back=None),
        )
    else:
        pending = (
            (
                await db.execute(
                    select(PartnerApplication).where(
                        PartnerApplication.telegram_id == actor(event), PartnerApplication.status == "pending"
                    )
                )
            )
            .scalars()
            .first()
        )
        if not pending:
            await registration_documents(event, dialog)
            return
        buttons = [("Проверить статус заявки", "main_menu")]
        if is_admin(actor(event)):
            buttons.append(("Администратор", "admin_menu"))
        await show(
            event,
            "Спасибо, ваша заявка уже на рассмотрении!\n\n"
            f"Номер заявки: {pending.id}\n\n"
            "Отправлять её повторно не нужно. Решение придёт в этот чат. "
            "Вернуться к проверке статуса можно в любой момент по кнопке ниже или командой /start.",
            keyboard(*buttons, back=None),
        )


async def handle_callback(event, db, dialog) -> None:
    data = event.data or ""
    user = actor(event)
    if data.startswith("admin_") and not is_admin(user):
        raise HTTPException(403, "admin_required")
    if dialog.state == "admin_application_key" and not data.startswith("confirm:"):
        from app.telegram.admin_applications import cancel_approvals

        await cancel_approvals(db, user, dialog.data["application_id"])
        reset(dialog)
    if data == "main_menu":
        await home(event, db, dialog)
        return
    if data.startswith("confirm:"):
        reset(dialog)
        await confirm_action(event, db, user, str(UUID(data.split(":", 1)[1])))
        return
    if data == "register":
        await home(event, db, dialog)
        return
    if data == "accept_legal":
        if dialog.state != "consent" or dialog.data.get("legal_version") != get_settings().legal_document_version:
            raise HTTPException(409, "consent_required")
        dialog.state = "register_company"
        await show(
            event,
            "Спасибо! Переходим к заявке на подключение.\n\n"
            "Шаг 1 из 2 — название компании.\n"
            "Отправьте его одним сообщением, от 2 до 255 символов. Например: «Студия Север».\n\n"
            "Чтобы начать заново или вернуться к документам, нажмите «Назад» или отправьте /cancel.",
        )
        return
    if data.startswith("admin_"):
        await admin_callback(event, db, dialog, data)
        return
    partner = await partner_for(db, user)
    if not partner:
        raise HTTPException(403, "account_unavailable")
    if data in {"legal_documents", "legal_accept_current"}:
        from app.telegram.legal import accept_current

        settings = get_settings()
        if not settings.terms_url or not settings.privacy_policy_url:
            raise HTTPException(503, "legal_not_configured")
        if data == "legal_accept_current":
            if dialog.state != "legal_review" or dialog.data.get("version") != settings.legal_document_version:
                raise HTTPException(409, "consent_required")
            await accept_current(db, partner)
            reset(dialog)
            await show(event, "Согласие с текущей версией документов сохранено.")
        else:
            dialog.state, dialog.data = "legal_review", {"version": settings.legal_document_version}
            await show(
                event,
                "Документы вашего подключения\n\n"
                "Здесь можно перечитать условия использования и политику конфиденциальности. "
                f"Текущая версия: {settings.legal_document_version}.\n\n"
                "Откройте оба документа по кнопкам ниже. Если вы согласны с текущей версией, "
                "нажмите «Принимаю оба документа» — мы сохраним ваше подтверждение.",
                keyboard(
                    ("Условия", settings.terms_url),
                    ("Политика конфиденциальности", settings.privacy_policy_url),
                    ("Принимаю оба документа", "legal_accept_current"),
                ),
            )
    elif data == "trials":
        from app.catalog.models import Model
        from app.telegram.models import TrialEntitlement

        used = await db.get(TrialEntitlement, user)
        remaining = 2 - used.used if used else 2
        models = (
            await db.execute(
                select(Model).where(Model.modality == "video", Model.status == "production").order_by(Model.slug)
            )
        ).scalars()
        buttons = [(m.name, f"trial_model:{m.id}") for m in models] if remaining else []
        await show(
            event,
            f"Попробуйте генерацию видео 🎬\nОсталось бесплатных запусков: {remaining} из 2.\n\n"
            "Выберите модель ниже, опишите видео и подтвердите запуск. Стоимость для вас — 0 ₽. "
            "Поддерживаются все параметры включённых видеомоделей, в том числе референсы через JSON.\n\n"
            "Принятый запуск расходует попытку, даже если генерация завершилась ошибкой. "
            "Повторная регистрация не восстанавливает тесты. Результат придёт в этот чат.",
            keyboard(*buttons),
        )
    elif data.startswith("trial_model:"):
        from app.catalog.models import Model

        model = await db.get(Model, str(UUID(data.split(":")[1])))
        if not model or model.modality != "video" or model.status != "production":
            raise HTTPException(404, "model_not_available")
        dialog.state, dialog.data = "trial_prompt", {"model": model.slug}
        await show(
            event,
            f"Вы выбрали {model.name}.\n\n"
            "Отправьте описание будущего видео: что происходит в кадре, какой нужен стиль, свет и движение камеры. "
            "Перед запуском попросим подтвердить запрос.\n\n"
            "Если нужны дополнительные настройки или референсы, отправьте полный JSON запроса "
            "videos/generations текстом или файлом .json до 1 МБ. Параметры модели сохраняются; model уже выбран. "
            "Чтобы выбрать другую модель, вернитесь в меню.",
        )
    elif data == "balance":
        reset(dialog)
        await show(
            event,
            f"Ваш баланс: {partner.balance_rub:.2f} ₽\n\n"
            "Для пополнения нажмите «Пополнить» и укажите сумму от 1 000 ₽. "
            "После подтверждения создадим счёт Crypto Pay с оплатой в USDT или TON. "
            "Рубли зачислятся автоматически после подтверждения платежа.\n\n"
            "В разделе «Мои счета» доступны ссылки на оплату и текущие статусы. "
            "Если вы уже оплатили счёт, откройте его и нажмите «Обновить».",
            keyboard(("Пополнить", "topup"), ("Мои счета", "payments:0")),
        )
    elif data == "topup":
        dialog.state, dialog.data = "topup", {}
        await show(
            event, "На какую сумму пополнить баланс?\n\n"
            "Введите целую сумму в рублях, от 1 000 ₽, без пробелов и знака валюты. Например: 3000. "
            "На следующем шаге покажем сумму для подтверждения перед созданием счёта. "
            "Отменить ввод можно командой /cancel.",
        )
    elif data.startswith("payments:"):
        page = page_number(data)
        rows = (
            (
                await db.execute(
                    select(PaymentInvoice)
                    .where(PaymentInvoice.partner_id == partner.id)
                    .order_by(PaymentInvoice.created_at.desc())
                    .offset(page * 4)
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
        buttons = [(f"{r.requested_rub:.2f} ₽ · {status_label(r.status)}", f"payment:{r.id}") for r in rows[:4]]
        navigation(buttons, "payments", page, len(rows) > 4)
        await show(
            event,
            "Ваши счета\n\nВыберите счёт, чтобы открыть ссылку на оплату, проверить статус или отменить его. "
            "Новые счета находятся в начале списка."
            if rows else "Счетов пока нет.\n\nКогда будете готовы, вернитесь в раздел баланса и нажмите «Пополнить». "
            "Перед созданием счёта вы сможете проверить сумму.",
            keyboard(*buttons),
        )
    elif data.startswith("payment:"):
        payment = await get_partner_payment(db, payment_id=str(UUID(data.split(":")[1])), partner_id=partner.id)
        if payment.status in {"creating", "creation_unknown"}:
            payment = await create_or_resume_invoice(
                db,
                partner=partner,
                requested_rub=int(payment.requested_rub),
                idempotency_key=payment.idempotency_key,
                client=get_crypto_pay_client(),
            )
        if payment.provider_invoice_id and payment.status in {"active", "expired", "creating"}:
            provider = await get_crypto_pay_client().get_invoice(payment.provider_invoice_id)
            if provider and provider.status == "paid":
                payment = await apply_paid_provider_invoice(db, provider_invoice=provider)
        buttons = [("Обновить", f"payment:{payment.id}")]
        if payment.status == "active":
            if payment.invoice_url:
                buttons.append(("Оплатить", payment.invoice_url))
            buttons.append(("Отменить счёт", f"payment_cancel:{payment.id}"))
        await show(
            event,
            f"Счёт {payment.id}\nСумма пополнения: {payment.requested_rub:.2f} ₽\n"
            f"Статус: {status_label(payment.status)}\n\n"
            "Кнопка «Обновить» проверяет текущее состояние счёта. "
            "Если оплата ещё доступна, ниже появится кнопка перехода в Crypto Pay. "
            "При обращении в поддержку можно указать номер этого счёта.",
            keyboard(*buttons),
        )
    elif data.startswith("payment_cancel:"):
        payment = await get_partner_payment(db, payment_id=str(UUID(data.split(":")[1])), partner_id=partner.id)
        await ask_confirmation(
            event,
            db,
            "invoice_cancel",
            {"partner_id": partner.id, "payment_id": payment.id},
            f"Отменить счёт на {payment.requested_rub:.2f} ₽?",
        )
    elif data.startswith("history:"):
        page = page_number(data)
        rows = (
            (
                await db.execute(
                    select(LedgerEntry)
                    .where(LedgerEntry.partner_id == partner.id)
                    .order_by(LedgerEntry.created_at.desc())
                    .offset(page * 8)
                    .limit(9)
                )
            )
            .scalars()
            .all()
        )
        labels = {
            "generation_reserve": "Резерв",
            "generation_charge": "Генерация",
            "payment_credit": "Пополнение",
            "generation_refund": "Возврат резерва",
        }
        lines = []
        for row in rows[:8]:
            generation_line = f"\nGeneration UUID: {row.generation_id}" if row.generation_id else ""
            lines.append(
                f"{row.created_at:%d.%m %H:%M} · {labels.get(row.operation_type, 'Операция')} · "
                f"{row.amount_rub:+.2f} ₽\nLedger UUID: {row.id}{generation_line}"
            )
        buttons = [("Поиск по UUID", "search_prompt")]
        navigation(buttons, "history", page, len(rows) > 8)
        await show(
            event, "История операций\n\n"
            "Здесь показаны пополнения, резервы, списания и возвраты. "
            "Для проверки генерации или счёта выберите «Поиск по UUID».\n\n"
            + ("\n".join(lines) or "Операций пока нет. После первого пополнения или запуска они появятся здесь."),
            keyboard(*buttons),
        )
    elif data == "search_prompt":
        dialog.state, dialog.data = "search", {}
        await show(event, "Давайте найдём нужную операцию.\n\n"
                   "Отправьте UUID генерации или платежа одним сообщением. "
                   "Его можно скопировать из ответа API или карточки счёта. "
                   "Покажем статус и сумму операции вашего аккаунта.")
    elif data.startswith("api_keys:"):
        reset(dialog)
        page = page_number(data)
        rows = (
            (
                await db.execute(
                    select(ApiKey)
                    .where(ApiKey.partner_id == partner.id, ApiKey.is_active.is_(True))
                    .order_by(ApiKey.created_at.desc())
                    .offset(page * 4)
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
        buttons = [(f"{r.name[:40]} · {r.key_prefix}…", f"key:{r.id}") for r in rows[:4]]
        buttons.append(("Создать ключ", "key_create"))
        navigation(buttons, "api_keys", page, len(rows) > 4)
        await show(
            event,
            ("Ваши действующие API-ключи" if rows else "Действующих ключей пока нет.")
            + "\n\nСоздайте отдельный ключ для каждого приложения, чтобы удобно управлять доступом. "
            "В карточке ключа можно настроить уведомления о генерациях или отозвать доступ. "
            "Полный новый ключ показывается при создании — сохраните его в защищённом хранилище.",
            keyboard(*buttons),
        )
    elif data == "key_create":
        dialog.state, dialog.data = "key_name", {}
        await show(event, "Как назовём новый API-ключ?\n\n"
                   "Введите понятное вам название от 2 до 120 символов, например «Сайт — продакшен». "
                   "Это поможет отличать приложения в списке. Затем попросим подтвердить создание.")
    elif data.startswith(("key:", "key_revoke:", "key_webhook:")):
        key = await db.get(ApiKey, str(UUID(data.split(":")[1])))
        if not key or key.partner_id != partner.id or not key.is_active:
            raise HTTPException(404, "api_key_not_found")
        if data.startswith("key_revoke:"):
            await ask_confirmation(
                event,
                db,
                "key_revoke",
                {"partner_id": partner.id, "key_id": key.id},
                f"Отозвать ключ «{key.name}»? Приложения с этим ключом потеряют доступ.",
            )
        elif data.startswith("key_webhook:"):
            dialog.state, dialog.data = "webhook", {"key_id": key.id}
            await show(
                event,
                "Настроим уведомления для вашего приложения.\n\n"
                "Отправьте публичный HTTPS-адрес, принимающий POST-запросы о состоянии генераций. "
                "Секрет для проверки подписи создадим автоматически и покажем после сохранения. "
                "Формат уведомлений описан в документации API.\n\n"
                "Чтобы отключить отправку, напишите «выключить». Для отмены ввода — /cancel.",
            )
        else:
            await show(
                event,
                f"{key.name}\n{key.key_prefix}…\nWebhook: {key.webhook_url or 'не задан'}",
                keyboard(("Webhook", f"key_webhook:{key.id}"), ("Отозвать", f"key_revoke:{key.id}"), back="api_keys:0"),
            )
    elif data == "settings":
        await show(event, "Документация и аккаунт\n\n"
                   "В руководстве API описаны подключение, доступные запросы и получение результатов. "
                   "Если нужна помощь с интеграцией, вернитесь в меню и откройте поддержку.\n\n"
                   "Удаление аккаунта — отдельное действие с подтверждением. "
                   "Перед ним покажем последствия для ключей, баланса и выполняющихся задач.",
                   keyboard(("Документация", "docs"), ("Удалить аккаунт", "delete")))
    elif data == "docs":
        await show(event, "Руководство по API Нейроныча\n\n"
                   "Начните с авторизации и примеров запросов, затем выберите нужную модель. "
                   "Для доступа понадобится API-ключ из соответствующего раздела кабинета.\n\n"
                   + get_settings().public_api_base_url.rstrip("/") + "/guide",
                   keyboard(("Открыть руководство", get_settings().public_api_base_url.rstrip("/") + "/guide")))
    elif data == "delete":
        await ask_confirmation(
            event,
            db,
            "delete",
            {"partner_id": partner.id},
            f"Удалить аккаунт? Ключи будут отключены, остаток {partner.balance_rub:.2f} ₽ невозвратный. "
            "Выполняющиеся задачи будут завершены и оплачены. История обязательных операций сохраняется.",
        )
    elif data.startswith(("support", "ticket:", "attachment:")):
        await support_callback(event, db, dialog, data, partner)
    else:
        await home(event, db, dialog)


def page_number(data: str) -> int:
    value = data.rsplit(":", 1)[1]
    if not value.isascii() or not value.isdigit() or len(value) > 6:
        raise ValueError("invalid page")
    return int(value)


def navigation(buttons: list, prefix: str, page: int, more: bool) -> None:
    if page:
        buttons.append(("← Предыдущие", f"{prefix}:{page - 1}"))
    if more:
        buttons.append(("Следующие →", f"{prefix}:{page + 1}"))


async def support_callback(event, db, dialog, data: str, partner=None) -> None:
    admin = is_admin(actor(event)) and data.startswith("admin_")
    if data in {"support", "admin_support"} or data.startswith(("support_page:", "admin_support_page:")):
        page = page_number(data) if ":" in data else 0
        query = select(SupportTicket)
        if not admin:
            query = query.where(SupportTicket.partner_id == partner.id)
        rows = (
            (await db.execute(query.order_by(SupportTicket.created_at.desc()).offset(page * 4).limit(5)))
            .scalars()
            .all()
        )
        buttons = [
            (f"{status_label(t.status)} · {t.subject[:38]}", f"{'admin_' if admin else ''}ticket:{t.id}:0")
            for t in rows[:4]
        ]
        if not admin:
            buttons.append(("Новое обращение", "support_new"))
        navigation(buttons, "admin_support_page" if admin else "support_page", page, len(rows) > 4)
        reset(dialog)
        await show(event, ("Обращения в поддержку" if rows else "Обращений пока нет.")
                   + "\n\nПоможем разобраться с подключением, генерациями и оплатой. "
                   "Откройте существующее обращение или создайте новое. "
                   "Для быстрого разбора укажите номер генерации или счёта и что вы ожидали получить. "
                   "Пожалуйста, не отправляйте API-ключи и другие секреты.", keyboard(*buttons))
    elif data == "support_new":
        dialog.state, dialog.data = "support_subject", {}
        await show(
            event,
            "Кратко опишите тему обращения (2–200 символов). Далее можно отправить сообщения и файлы до 20 МБ каждый.",
        )
    elif data.startswith(("ticket:", "admin_ticket:")):
        _, ticket_id, page_value = data.split(":")
        ticket = await get_ticket(db, str(UUID(ticket_id)), actor(event))
        page = page_number("page:" + page_value)
        rows = await ticket_messages(db, ticket.id, page * 4)
        lines = [f"Обращение {ticket.id}\n{ticket.subject}\n{status_label(ticket.status)}"]
        buttons = []
        for row in reversed(rows[:4]):
            lines.append(f"{'Поддержка' if row.sender_type == 'admin' else 'Вы'}: {row.text[:650]}")
            attachments = (
                (await db.execute(select(SupportAttachment).where(SupportAttachment.message_id == row.id)))
                .scalars()
                .all()
            )
            buttons.extend(
                (f"Файл: {item.file_name[:35]}", f"{'admin_' if is_admin(actor(event)) else ''}attachment:{item.id}")
                for item in attachments
            )
        prefix = "admin_ticket" if is_admin(actor(event)) else "ticket"
        if page:
            buttons.append(("Новые сообщения", f"{prefix}:{ticket.id}:{page - 1}"))
        if len(rows) > 4:
            buttons.append(("Ранее", f"{prefix}:{ticket.id}:{page + 1}"))
        if ticket.status != "closed":
            dialog.state, dialog.data = (
                "admin_support_reply" if is_admin(actor(event)) else "support_reply",
                {"ticket_id": ticket.id},
            )
            lines.append("Отправьте сообщение или файл, чтобы ответить.")
            if is_admin(actor(event)):
                buttons.append(("Закрыть обращение", f"admin_ticket_close:{ticket.id}"))
        else:
            reset(dialog)
        await show(
            event, "\n\n".join(lines), keyboard(*buttons, back="admin_support" if is_admin(actor(event)) else "support")
        )
    elif data.startswith(("attachment:", "admin_attachment:")):
        attachment = await attachment_for(db, str(UUID(data.split(":")[1])), actor(event))
        await event.message.answer_document(FSInputFile(attachment.storage_path, filename=attachment.file_name))
    elif data.startswith("admin_ticket_close:"):
        ticket = await get_ticket(db, str(UUID(data.split(":")[1])), actor(event))
        await ask_confirmation(
            event,
            db,
            "admin_ticket_close",
            {"ticket_id": ticket.id},
            "Закрыть обращение? Для следующего вопроса потребуется новое.",
        )


async def admin_callback(event, db, dialog, data: str) -> None:
    if data == "admin_ops":
        from app.telegram.admin_forms import menu

        reset(dialog)
        await menu(event)
    elif data.startswith("admin_form:"):
        from app.telegram.admin_forms import start

        await start(event, db, dialog, data.split(":", 1)[1])
    elif data.startswith("admin_partner_"):
        from app.telegram.admin_partners import handle_picker_callback

        await handle_picker_callback(event, db, dialog, data)
    elif data.startswith("admin_partners:"):
        from app.telegram.admin_partners import show_admin_partners

        await show_admin_partners(event, db, dialog, page_number(data))
    elif data == "admin_partners_search":
        from app.telegram.admin_partners import prompt_admin_partner_search

        await prompt_admin_partner_search(event, dialog)
    elif data == "admin_partners_all":
        from app.telegram.admin_partners import show_admin_partners

        reset(dialog)
        await show_admin_partners(event, db, dialog, 0, search=None)
    elif data == "admin_catalog_import":
        await ask_confirmation(
            event,
            db,
            "admin_catalog_import",
            {},
            "Сверить live-каталог и импортировать reviewed модели/себестоимость? "
            "Новые модели останутся черновиками. Розничные цены сохраняются.",
        )
    elif data.startswith(("admin_catalog:", "admin_threshold_history:", "admin_reconcile_list", "admin_credentials:")):
        from app.telegram.admin_forms import history

        await history(event, db, data)
    elif data == "admin_menu":
        reset(dialog)
        await show(
            event,
            "Администрирование",
            keyboard(
                ("Заявки", "admin_apps:0"),
                ("Платежи", "admin_payments:0"),
                ("Казначейство", "admin_stw"),
                ("Цены, возвраты и сверки", "admin_ops"),
                ("InfAI: модели и цены", "admin_infai"),
                ("Поддержка", "admin_support"),
                ("Партнёры", "admin_partners:0"),
                ("Состояние", "admin_health"),
            ),
        )
    elif data == "admin_infai":
        from app.telegram.infai import overview

        reset(dialog)
        await overview(event, db)
    elif data.startswith("admin_apps:"):
        page = page_number(data)
        rows = (
            (
                await db.execute(
                    select(PartnerApplication)
                    .where(PartnerApplication.status == "pending")
                    .order_by(PartnerApplication.created_at)
                    .offset(page * 4)
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
        buttons = [(r.company_name[:45], f"admin_app:{r.id}") for r in rows[:4]]
        navigation(buttons, "admin_apps", page, len(rows) > 4)
        await show(
            event, "Заявки на подключение" if rows else "Новых заявок нет.", keyboard(*buttons, back="admin_menu")
        )
    elif data.startswith(("admin_app:", "admin_approve:", "admin_reject:", "admin_app_key:")):
        from app.telegram.admin_applications import (
            begin_approval,
            cancel_approvals,
            credential_for,
            key_dialog,
            request_key,
        )

        app = await db.get(PartnerApplication, str(UUID(data.split(":")[1])))
        if not app or app.status != "pending":
            raise HTTPException(404, "application_not_found")
        if data.startswith("admin_approve:"):
            await begin_approval(event, db, dialog, app)
        elif data.startswith("admin_app_key:"):
            await request_key(event, db, dialog, app)
        elif data.startswith("admin_reject:"):
            dialog.state, dialog.data = "admin_reject", {"application_id": app.id}
            await show(event, "Введите причину отказа — она будет отправлена заявителю.")
        else:
            await cancel_approvals(db, actor(event), app.id)
            key_dialog(dialog, app.id)
            credential = await credential_for(db, app.id)
            key_status = "Ключ поставщика привязан. Для замены пришлите другой ключ." if credential else (
                "Пришлите ключ поставщика сообщением — он будет проверен для этой заявки."
            )
            buttons = [("Одобрить", f"admin_approve:{app.id}")] if credential else []
            buttons += [
                ("Заменить ключ" if credential else "Добавить ключ", f"admin_app_key:{app.id}"),
                ("Отклонить", f"admin_reject:{app.id}"),
            ]
            await show(
                event,
                f"Заявка {app.id}\n{app.company_name}\n{app.project_name}\n"
                f"Telegram ID: {app.telegram_id}\n\n{key_status}",
                keyboard(*buttons, back="admin_apps:0"),
            )
    elif data.startswith("admin_payments:"):
        page = page_number(data)
        rows = (
            (
                await db.execute(
                    select(PaymentInvoice)
                    .where(PaymentInvoice.status == "paid_waiting_credit")
                    .order_by(PaymentInvoice.created_at)
                    .offset(page * 4)
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
        buttons = [(f"{r.requested_rub:.2f} ₽ · {r.id[:8]}", f"admin_credit:{r.id}") for r in rows[:4]]
        navigation(buttons, "admin_payments", page, len(rows) > 4)
        await show(
            event,
            "Оплаченные счета на зачисление" if rows else "Нет счетов на зачисление.",
            keyboard(*buttons, back="admin_menu"),
        )
    elif data.startswith("admin_credit:"):
        payment = await db.get(PaymentInvoice, str(UUID(data.split(":")[1])))
        if not payment:
            raise HTTPException(404, "payment_not_found")
        await ask_confirmation(
            event,
            db,
            "admin_credit",
            {"payment_id": payment.id},
            f"Зачислить {payment.requested_rub:.2f} ₽?\nПартнёр: {payment.partner_id}\n"
            f"Платёж: {payment.id}\n{status_label(payment.status)}",
        )
    elif data == "admin_stw":
        state = await calculate_safe_to_withdraw(db)
        value = state["safe_to_withdraw_usdt"]
        freshness = "актуальные данные" if state["freshness"] == "fresh" else "данные устарели или недоступны"
        amount = format(value, ".2f") + " USDT" if value is not None else "нет подтверждённых данных"
        await show(
            event,
            f"Доступно к выводу: {amount}\n"
            f"Кошелёк: {freshness}\n"
            f"Возраст: {int(state.get('wallet_age_seconds') or 0)} с",
            keyboard(("Курс RUB/USDT", "admin_fx"), ("Заглушить инцидент", "admin_mute"), back="admin_menu"),
        )
    elif data == "admin_fx":
        from app.billing.fx import current_fx, fx_policy

        fx = await current_fx(db)
        policy = await fx_policy(db)
        automatic = policy["automatic_enabled"]
        mode = "автоматический" if automatic else "ручной"
        buttons = [("Ручной режим / задать курс", "admin_fx_manual")]
        if not automatic:
            buttons.insert(0, ("Включить авто", "admin_fx_auto"))
        await show(
            event,
            f"Курс: {fx['rate']} ₽/USDT. Источник: {fx['source']}.\n"
            f"Режим: {mode}.\n\n"
            "Авто запрашивает Crypto Pay только при создании нового счёта. "
            "Все остальные операции используют сохранённый курс.",
            keyboard(*buttons, back="admin_stw"),
        )
    elif data == "admin_fx_auto":
        await ask_confirmation(
            event,
            db,
            "admin_fx",
            {"automatic_enabled": True},
            "Включить автоматический курс? Новый курс будет запрашиваться только при создании нового счёта.",
        )
    elif data == "admin_fx_manual":
        dialog.state, dialog.data = "admin_fx_manual", {}
        await show(event, "Введите ручной курс RUB/USDT, например 95.50.", keyboard(back="admin_fx"))
    elif data == "admin_mute":
        from app.billing.models import FinancialIncident

        incident = await db.get(FinancialIncident, "treasury")
        if not incident:
            await show(event, "Открытых инцидентов нет.")
        else:
            await ask_confirmation(
                event,
                db,
                "admin_mute",
                {"episode": incident.episode},
                "Остановить уведомления по текущему инциденту? При новом дефиците они включатся снова.",
            )
    elif data == "admin_health":
        import json

        from app.health.router import readiness

        result = json.loads((await readiness(db)).body)
        from app.providers.models import ProviderCircuit

        circuits = (await db.execute(select(ProviderCircuit))).scalars()
        details = "\n".join(
            f"{row.provider}: {row.state}, бесплатных проверок {row.healthy_checks}/3, "
            f"реальных проб {row.real_successes}/3"
            for row in circuits
        )
        await show(
            event,
            "Состояние сервиса\n" + "\n".join(f"{k}: {v}" for k, v in result["checks"].items()) + "\n" + details,
            keyboard(back="admin_menu"),
        )
    elif data == "admin_account":
        dialog.state, dialog.data = "admin_account", {}
        await show(event, "Введите UUID партнёра для изменения доступа или переноса Telegram ID.")
    elif data.startswith(("admin_disable:", "admin_enable:", "admin_transfer:")):
        partner_id = str(UUID(data.split(":")[1]))
        dialog.state, dialog.data = (
            "admin_transfer" if data.startswith("admin_transfer:") else "admin_status",
            {"partner_id": partner_id, "enabled": data.startswith("admin_enable:")},
        )
        await show(
            event,
            "Введите новый Telegram ID и причину переноса через пробел. Подтвердите личность владельца до переноса."
            if dialog.state == "admin_transfer"
            else "Введите причину изменения доступа.",
        )
    elif data.startswith(("admin_support", "admin_ticket", "admin_attachment")):
        await support_callback(event, db, dialog, data)


async def handle_message(event, db, dialog) -> None:
    value = (event.text or "").strip()
    if value.split(" ", 1)[0].split("@", 1)[0] in {"/start", "/cancel"}:
        await home(event, db, dialog)
        return
    user = actor(event)
    state = dialog.state
    if state.startswith("admin_") and not is_admin(user):
        reset(dialog)
        raise HTTPException(403, "admin_required")
    if state in {"register_company", "register_project"}:
        settings = get_settings()
        if (dialog.data.get("legal_version") != settings.legal_document_version
                or not settings.terms_url or not settings.privacy_policy_url):
            await registration_documents(event, dialog)
            return
    if state == "register_company":
        if not 2 <= len(value) <= 255:
            raise ValueError("company length")
        dialog.data = {**dialog.data, "company": value}
        dialog.state = "register_project"
        await show(
            event,
            "Отлично, название компании записано.\n\n"
            "Шаг 2 из 2 — ваш проект.\n"
            "Одним сообщением укажите название и кратко расскажите, для чего хотите использовать API "
            "(от 2 до 255 символов). Например: «Север Видео — создание роликов для интернет-магазинов».\n\n"
            "После этого сообщения отправим заявку на рассмотрение. "
            "Если хотите исправить первый шаг, отправьте /cancel и заполните анкету заново.",
        )
        return
    if state == "register_project":
        if not 2 <= len(value) <= 255:
            raise ValueError("project length")
        app = await submit_application(
            PartnerApplicationCreate(
                telegram_id=user,
                company_name=dialog.data["company"],
                project_name=value,
                accepted_terms=True,
                accepted_privacy_policy=True,
                terms_version=dialog.data["legal_version"],
                privacy_policy_version=dialog.data["legal_version"],
            ),
            db,
        )
        await notify(
            db,
            get_settings().admin_telegram_id,
            f"Новая заявка {app.id}. Откройте раздел «Заявки».",
            f"application:{app.id}",
        )
        reset(dialog)
        await db.commit()
        await show(
            event,
            f"Спасибо! Заявка {app.id} принята.\n\n"
            "Мы получили данные компании, описание проекта и согласие с документами. "
            "Решение придёт в этот чат — повторно заполнять анкету не нужно. "
            "Проверить статус можно командой /start или кнопкой ниже.",
            keyboard(("Проверить статус заявки", "main_menu"), back=None),
        )
        return
    partner = await partner_for(db, user)
    if not partner and not state.startswith("admin_"):
        await home(event, db, dialog)
        return
    if state == "admin_application_key":
        from app.telegram.admin_applications import receive_key

        await receive_key(event, db, dialog, value)
    elif state == "admin_partner_pick":
        from app.telegram.admin_partners import search_partners

        await search_partners(event, db, dialog, value)
    elif state == "admin_partner_browse_search":
        from app.telegram.admin_partners import search_admin_partners

        await search_admin_partners(event, db, dialog, value)
    elif state == "admin_form_input":
        from app.telegram.admin_forms import input_value

        await input_value(event, db, dialog, value)
    elif state == "trial_prompt":
        import json

        from app.contracts.registry import validate_request

        if event.document:
            from app.support.service import BoundedBuffer

            if (event.document.file_size or 0) > 1024 * 1024:
                raise ValueError("trial_file_too_large")
            output = BoundedBuffer(1024 * 1024)
            await event.bot.download(event.document, destination=output)
            value = output.getvalue().decode("utf-8")
        body = json.loads(value) if value.startswith("{") else {"prompt": value}
        if not isinstance(body, dict):
            raise ValueError("invalid_request")
        if not value:
            raise ValueError("empty_prompt")
        body["model"] = dialog.data["model"]
        body = validate_request("videos/generations", body)
        reset(dialog)
        await ask_confirmation(
            event,
            db,
            "trial",
            {"partner_id": partner.id, "body": body},
            f"Запустить бесплатный тест {body['model']}? Стоимость 0 ₽. "
            "Будет использован один из двух пробных запусков.",
        )
    elif state == "search":
        identifier = str(UUID(value))
        row = (
            await db.execute(select(Generation).where(Generation.id == identifier, Generation.partner_id == partner.id))
        ).scalar_one_or_none()
        if row:
            from app.telegram.trials import download_link

            charge = row.actual_charge_rub if row.actual_charge_rub is not None else row.partner_price_rub
            result = (
                "\n" + download_link(row)
                if row.status == "completed" and (row.request_payload or {}).get("trial_telegram_id")
                else ""
            )
            await show(
                event,
                f"Генерация {row.id}\n{status_label(row.status)}\nСумма: {charge:.2f} ₽{result}",
            )
        else:
            payment = (
                await db.execute(
                    select(PaymentInvoice).where(
                        PaymentInvoice.id == identifier, PaymentInvoice.partner_id == partner.id
                    )
                )
            ).scalar_one_or_none()
            await show(
                event,
                f"Платёж {payment.id}\n{status_label(payment.status)}\n{payment.requested_rub:.2f} ₽"
                if payment
                else "В вашем аккаунте такой операции нет.",
            )
    elif state == "topup":
        if not value.isascii() or not value.isdigit() or not 1000 <= int(value) <= 9999999999999999:
            raise ValueError("amount")
        reset(dialog)
        await ask_confirmation(
            event,
            db,
            "invoice_create",
            {"partner_id": partner.id, "amount": value},
            f"Создать счёт на {int(value):,} ₽? Срок оплаты — 1 час.",
        )
    elif state == "key_name":
        if not 2 <= len(value) <= 120:
            raise ValueError("name")
        reset(dialog)
        await ask_confirmation(
            event, db, "key_create", {"partner_id": partner.id, "name": value}, f"Создать API-ключ «{value}»?"
        )
    elif state == "webhook":
        import secrets

        secret = None if value.lower() == "выключить" else secrets.token_urlsafe(32)
        await update_partner_api_key_webhook(
            partner.id,
            dialog.data["key_id"],
            ApiKeyWebhookUpdate(webhook_url=value if secret else None, webhook_secret=secret),
            db,
        )
        reset(dialog)
        await db.commit()
        await show(event, f"Webhook настроен. Сохраните секрет подписи:\n{secret}" if secret else "Webhook отключён.")
    elif state == "support_subject":
        if not 2 <= len(value) <= 200:
            raise ValueError("subject")
        ticket = SupportTicket(partner_id=partner.id, subject=value)
        db.add(ticket)
        await db.flush()
        dialog.state, dialog.data = "support_reply", {"ticket_id": ticket.id}
        await notify(
            db, get_settings().admin_telegram_id, f"Новое обращение {ticket.id}: {value}", f"ticket:{ticket.id}"
        )
        await db.commit()
        await show(
            event,
            f"Обращение {ticket.id} создано. Отправьте сообщение, скриншот или файл (до 20 МБ каждый).",
            keyboard(back="support"),
        )
    elif state in {"support_reply", "admin_support_reply"}:
        ticket = await get_ticket(db, dialog.data["ticket_id"], user)
        await append_message(db, ticket, event, telegram_id=user)
        await db.commit()
        await show(
            event,
            "Сообщение добавлено. Можно отправить ещё одно сообщение или файл.",
            keyboard(back="admin_support" if is_admin(user) else "support"),
        )
    elif state in {"admin_fx", "admin_fx_manual"}:
        from decimal import Decimal

        if state == "admin_fx" and value.lower() == "выключить":
            payload = {"automatic_enabled": True}
            prompt = "Оставить автоматический режим без ручного fallback-курса?"
        else:
            rate = Decimal(value.replace(",", "."))
            if not rate.is_finite() or not 0 < rate < Decimal("1000000000000"):
                raise ValueError("invalid rate")
            payload = {"automatic_enabled": False, "rate": str(rate)}
            prompt = f"Включить ручной курс {rate} ₽/USDT? Автоматические запросы курса будут отключены."
        reset(dialog)
        await ask_confirmation(event, db, "admin_fx", payload, prompt)
    elif state == "admin_reject":
        if not 1 <= len(value) <= 2000:
            raise ValueError("reason")
        payload = {**dialog.data, "reason": value}
        reset(dialog)
        await ask_confirmation(event, db, "admin_reject", payload, f"Отклонить заявку? Причина: {value}")
    elif state == "admin_account":
        from app.accounts.models import Partner

        owner = await db.get(Partner, str(UUID(value)))
        if not owner or owner.status == "deleted":
            raise HTTPException(404, "partner_not_found")
        reset(dialog)
        await show(
            event,
            f"{owner.company_name}\n{owner.id}\nTelegram: {owner.telegram_id}\nБаланс: {owner.balance_rub:.2f} ₽",
            keyboard(
                (
                    "Отключить" if owner.status == "active" else "Включить",
                    f"admin_{'disable' if owner.status == 'active' else 'enable'}:{owner.id}",
                ),
                ("Перенести Telegram ID", f"admin_transfer:{owner.id}"),
            ),
        )
    elif state in {"admin_transfer", "admin_status"}:
        payload = dict(dialog.data)
        if state == "admin_transfer":
            from app.accounts.schemas import TransferTelegramCreate

            target, reason = value.split(" ", 1)
            valid = TransferTelegramCreate(telegram_id=target, reason=reason)
            payload.update(valid.model_dump())
        else:
            if not 3 <= len(value) <= 2000:
                raise ValueError("reason")
            payload["reason"] = value
        reset(dialog)
        await ask_confirmation(
            event, db, state, payload, f"Подтвердить изменение аккаунта {payload['partner_id']}?\n{value}"
        )
    else:
        await home(event, db, dialog)
