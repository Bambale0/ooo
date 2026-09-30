"""Public product overview: no account data, catalog dependency, or paid requests."""

from html import escape

from app.api.public_ui import page
from app.infrastructure.config import get_settings


def marketing_page(lang: str):
    def t(ru, en):
        return ru if lang == "ru" else en

    docs = f"/guide?lang={lang}"
    body = (
        '<section class="marketing-section" aria-labelledby="possibilities">'
        f'<p class="eyebrow">{t("Возможности", "Capabilities")}</p>'
        f'<h2 id="possibilities">{t("От ответа до целой истории", "From an answer to a complete story")}</h2>'
        f"<p>{t('Генерация для приложений, ботов и команд.', 'Generation for apps, bots and workflows.')}</p>"
        '<div class="capability-grid">'
    )
    for number, title, description, anchor, endpoint in (
        (
            "01",
            t("Текст", "Text"),
            t(
                "Ассистенты, ответы на вопросы, работа с документами и создание контента.",
                "Assistants, question answering, document workflows and content creation.",
            ),
            "text",
            "/v1/chat/completions",
        ),
        (
            "02",
            t("Изображения", "Images"),
            t(
                "Визуалы по описанию, редактирование изображений и работа с референсами.",
                "Create visuals from prompts, edit images and work with references.",
            ),
            "images",
            "/v1/images/generations",
        ),
        (
            "03",
            t("Видео", "Video"),
            t(
                "Генерация роликов по тексту и референсам с получением статуса и результата.",
                "Generate videos from text and references, then retrieve status and results.",
            ),
            "video",
            "/v1/videos/generations",
        ),
    ):
        body += (
            f'<a class="capability-card" href="{docs}#{anchor}"><span class="card-number">{number} /</span>'
            f"<h3>{title}</h3><p>{description}</p><code>{endpoint}</code>"
            f'<span class="card-link">{t("Как подключить", "Explore the API")} ↗</span></a>'
        )
    body += '</div></section><section class="integration-section" aria-labelledby="first-request"><div>'
    body += (
        f'<p class="eyebrow">{t("Для разработчиков", "For developers")}</p>'
        f'<h2 id="first-request">{t("Первый шаг —<br>один запрос", "One request.<br>Your first step.")}</h2>'
        f"<p>{
            t(
                'Получите список доступных моделей. Этот запрос не требует ключа и не запускает генерацию.',
                'Discover available models. This request needs no API key and does not start a generation.',
            )
        }</p>"
        f'<a class="button" href="{docs}#models">{t("Открыть справочник", "Open the reference")} →</a></div><div>'
    )
    example = f'curl "{get_settings().public_api_base_url.rstrip("/")}/v1/models"'
    body += f'<pre><code>{escape(example)}</code></pre><div class="integration-note">'
    body += (
        f"<strong>{t('Вы управляете интеграцией', 'You control the integration')}</strong>"
        f"<p>{
            t(
                'Выбирайте модель и параметры запроса. Ключи храните на своём сервере. '
                'Примеры запросов и правила повторов — в документации.',
                'Choose the model and request parameters. Keep API keys on your server. '
                'Find request examples and retry rules in the documentation.',
            )
        }</p></div></div></section>"
        '<section class="marketing-section" aria-labelledby="getting-started">'
        f'<p class="eyebrow">{t("Подключение", "Getting started")}</p>'
        f'<h2 id="getting-started">{t("Путь к вашей первой генерации", "Your path to the first generation")}</h2>'
        '<ol class="onboarding-steps">'
    )
    for title, text in (
        (
            t("Подключитесь как партнёр", "Become a partner"),
            t(
                "Для доступа нужны одобренный аккаунт и API-ключ.",
                "Access requires an approved account and an API key.",
            ),
        ),
        (
            t("Выберите модель", "Choose a model"),
            t(
                "Сверьте возможности, параметры и цены под свою задачу.",
                "Compare capabilities, parameters and prices for your task.",
            ),
        ),
        (
            t("Отправьте запрос", "Send a request"),
            t(
                "Пополните баланс и используйте примеры из документации.",
                "Fund your balance and use the documented examples.",
            ),
        ),
    ):
        body += f"<li><h3>{title}</h3><p>{text}</p></li>"
    body += (
        '</ol></section><section class="marketing-cta">'
        f'<div><p class="eyebrow">{t("Понятная экономика", "Clear billing")}</p>'
        f"<h2>{t('Один баланс. Расчёты в рублях.', 'One balance. Billing in rubles.')}</h2>"
        f"<p>{
            t(
                'Цены по моделям и конфигурациям открыты. Цену для своих клиентов определяете вы.',
                'Model and configuration prices are public. You set the price for your own customers.',
            )
        }</p></div>"
        f'<a class="button primary" href="/price">{t("Посмотреть цены", "View prices")} ↗</a></section>'
    )
    return page(
        t("Нейроныч API — нейросети для вашего продукта", "Neironych API — AI for your product"),
        body,
        lang,
        landing=True,
    )
