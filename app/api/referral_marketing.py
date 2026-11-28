"""Public partner program; rates are shared with the referral ledger."""

from app.api.public_ui import page
from app.referrals.service import API_TOPUP_RATE, DEVELOPMENT_ORDER_RATE, rate_percent


def referral_marketing_page():
    api_rate = rate_percent(API_TOPUP_RATE)
    development_rate = rate_percent(DEVELOPMENT_ORDER_RATE)
    body = (
        '<section class="marketing-section" aria-labelledby="rewards">'
        '<p class="eyebrow">01 / Вознаграждение</p><h2 id="rewards">Два направления сотрудничества</h2>'
        '<p>Рекомендуйте те решения, которые подходят вашей аудитории.</p>'
        '<div class="capability-grid referral-reward-grid">'
        '<article class="capability-card"><span class="card-number">API · ПОЖИЗНЕННО</span>'
        f'<strong class="referral-rate">{api_rate}</strong><h3>От пополнений API</h3>'
        '<p>Вознаграждение начисляется с каждого подтверждённого пополнения API привлечённого клиента. '
        'Повторные пополнения также учитываются.</p></article>'
        '<article class="capability-card"><span class="card-number">РАЗРАБОТКА · ЗА ЗАКАЗ</span>'
        f'<strong class="referral-rate">{development_rate}</strong><h3>От заказов на разработку</h3>'
        '<p>Получайте вознаграждение за приведённого заказчика после полной оплаты заказа '
        'на разработку.</p></article></div></section>'
        '<section class="marketing-section" aria-labelledby="how">'
        '<p class="eyebrow">02 / Как это работает</p><h2 id="how">От рекомендации до начисления</h2>'
        '<ol class="onboarding-steps">'
        '<li><h3>Получите ссылку</h3><p>Войдите в личный кабинет и откройте раздел '
        '«Партнёрская программа».</p></li>'
        '<li><h3>Приглашайте клиентов</h3><p>Поделитесь персональной ссылкой с клиентами '
        'и вашей аудиторией. Она ведёт в Telegram-бота.</p></li>'
        '<li><h3>Следите за результатом</h3><p>Смотрите количество приглашений и историю '
        'начислений в веб-кабинете.</p></li></ol></section>'
        '<section class="marketing-section" aria-labelledby="audience">'
        '<p class="eyebrow">03 / Для кого</p><h2 id="audience">Создавайте ценность вместе с нами</h2>'
        '<div class="capability-grid">'
        '<article class="capability-card"><h3>Разработчикам</h3>'
        '<p>Рекомендуйте API командам, которые создают ботов, приложения и AI-сервисы.</p></article>'
        '<article class="capability-card"><h3>Агентствам и интеграторам</h3>'
        '<p>Предлагайте решения для автоматизации, AI-интеграции и разработки вашим клиентам.</p></article>'
        '<article class="capability-card"><h3>Авторам и сообществам</h3>'
        '<p>Рассказывайте о платформе аудитории, которая интересуется нейросетями и технологиями.</p></article>'
        '</div></section>'
        '<section class="marketing-section" aria-labelledby="faq">'
        '<p class="eyebrow">04 / Условия</p><h2 id="faq">Частые вопросы</h2>'
        '<div class="referral-faq">'
        '<details><summary>Как закрепляется реферал?</summary><p>При первом посещении Telegram-бота '
        'по действительной партнёрской ссылке. В дальнейшем привязка не меняется. '
        'Самоприглашения и повторная привязка не учитываются.</p></details>'
        '<details><summary>Когда начисляется вознаграждение?</summary>'
        '<p>По API — после подтверждённого пополнения. По разработке — после полной оплаты заказа. '
        'Возвраты могут корректировать ранее начисленные суммы.</p></details>'
        '<details><summary>Как посмотреть статистику?</summary>'
        '<p>Откройте раздел «Партнёрская программа» в веб-кабинете. Там доступны ваша ссылка, '
        'количество приглашений и история начислений в рублях.</p></details>'
        '<details><summary>Как получить вознаграждение?</summary>'
        '<p>Начисления отображаются отдельно от API-баланса. По вопросу получения '
        'вознаграждения обратитесь в Telegram-поддержку.</p></details>'
        '</div></section>'
        '<section class="marketing-cta"><div><p class="eyebrow">Начните сотрудничество</p>'
        '<h2>Ваша первая рекомендация начинается со ссылки</h2>'
        '<p>Войдите в действующий аккаунт, получите персональную ссылку и поделитесь ею.</p></div>'
        '<a class="button primary" href="/cabinet?section=referrals">В партнёрский кабинет →</a></section>'
    )
    return page(
        "Партнёрская программа Нейроныч API — реферальные вознаграждения",
        body,
        partners=True,
        seo_description=(
            f"Партнёрская программа Нейроныч: {api_rate} от пополнений API пожизненно "
            f"и {development_rate} от полностью оплаченных заказов на разработку. Личный кабинет партнёра."
        ),
    )
