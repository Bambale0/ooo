"""Administrator-only provider inventory; credentials never enter Telegram."""

from app.providers.infai import InfaiError, catalog_with_retail
from app.telegram.ui import keyboard, show


async def overview(event, db) -> None:
    try:
        report = await catalog_with_retail(db)
    except InfaiError as error:
        text = {
            "infai_not_configured": (
                "Для подключения нужны системный токен InfAI и числовой ID аккаунта в настройках сервера."
            ),
            "infai_auth_failed": "InfAI отклонил авторизацию. Проверьте системный токен и ID аккаунта на сервере.",
        }.get(error.code, "Не удалось получить свежие данные InfAI. Пожалуйста, попробуйте обновить раздел позже.")
        await show(event, text, keyboard(("Обновить", "admin_infai"), back="admin_menu"))
        return
    counts, account = report["counts"], report["account"]
    await show(
        event,
        f"InfAI — состояние аккаунта и каталог\n\n"
        f"Остаток: ${account['balance_usd']:.5f}\n"
        f"Учтённый расход: ${account['used_usd']:.5f}\n"
        f"Группа аккаунта: {account['group']}\n"
        f"Доступных групп: {len(report['groups']['data'])}\n"
        f"Моделей аккаунта: {counts['account_models']}\n"
        f"Записей в прайсе: {counts['priced_models']}\n"
        f"Без опубликованной цены: {counts['unpriced_account_models']}\n"
        f"Точных совпадений с нашим каталогом: {counts['exact_local_matches']}\n\n"
        "Коэффициенты и закупочные ставки различаются по группам и моделям. "
        "Подробная таблица доступна в административном API /api/v1/providers/infai/catalog "
        "и через экспорт app.providers.infai_export. Каждая группа показана отдельно.\n\n"
        "Розничные цены сохранены на текущем уровне ArgoLink. "
        "Этот раздел читает данные; генерации через InfAI пока не включены.",
        keyboard(("Обновить", "admin_infai"), ("Прайс InfAI", "https://infai.cc/pricing"), back="admin_menu"),
    )
