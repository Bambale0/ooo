from app.infrastructure.config import get_settings
from app.referrals.service import get_or_create_profile, referral_stats
from app.telegram.ui import keyboard, show


async def show_program(event, db, partner) -> None:
    profile = await get_or_create_profile(db, partner.id)
    stats = await referral_stats(db, partner.id)
    username = (get_settings().telegram_bot_username or "").lstrip("@")
    link = f"https://t.me/{username}?start=ref_{profile.code}" if username else None
    text = (
        "🤝 Партнёрская программа Нейроныча\n\n"
        "• 20% от оплаченного заказа на разработку.\n"
        "• 7% от каждого пополнения API приглашённого пользователя — пожизненно.\n\n"
        f"Приглашено: {stats['invited']}\n"
        f"Начислено: {stats['balance_rub']:.2f} ₽\n\n"
        "Реферал закрепляется за вами при первом переходе и не меняется."
    )
    buttons = []
    if link:
        text += f"\n\nВаша ссылка:\n{link}"
        buttons.append(("Открыть партнёрскую ссылку", link))
    else:
        text += f"\n\nВаш код: {profile.code}\nДобавьте TELEGRAM_BOT_USERNAME, чтобы показывать готовую ссылку."
    await show(event, text, keyboard(*buttons) if buttons else keyboard())
