"""Partner-safe model reference shared by Telegram and web integration screens."""

import asyncio
import json
from dataclasses import dataclass
from functools import lru_cache
from html import escape
from urllib.parse import urlencode

from fastapi import HTTPException, Response
from sqlalchemy import and_, or_, select

from app.api.native_video_reference import EXAMPLES as NATIVE_VIDEO_EXAMPLES
from app.api.native_video_reference import LIMITS as NATIVE_VIDEO_LIMITS
from app.api.partner_reference import EXAMPLES as MEDIA_EXAMPLES
from app.api.partner_reference import VIDEO_LIMITS
from app.api.text_reference import EXAMPLES as TEXT_EXAMPLES
from app.api.text_reference import PUBLIC_TEXT_MODELS
from app.catalog.models import Model, PartnerModelGrant
from app.contracts.registry import MODELS, image_reference_limit, validate_request

CATEGORY_LABELS = {"chat": "Текст", "image": "Изображения", "video": "Видео"}
TEXT_FIELDS = {
    "context_tokens": "Контекст, токенов",
    "max_input_tokens": "Максимум входных токенов",
    "max_output_tokens": "Максимум выходных токенов",
    "default_output_tokens": "Выходных токенов по умолчанию",
    "images": "Анализ изображений",
    "efforts": "Уровни рассуждений",
    "default_effort": "Уровень рассуждений по умолчанию",
    "effort_aliases": "Преобразование уровней рассуждений",
    "thinking": "Режим рассуждений",
}
IMAGE_FIELDS = {
    "frame": "Параметр размера",
    "resolutions": "Разрешения",
    "default_resolution": "Разрешение по умолчанию",
    "aspect_ratios": "Соотношения сторон",
    "default_aspect_ratio": "Соотношение сторон по умолчанию",
    "quality": "Уровни качества",
    "default_quality": "Качество по умолчанию",
    "max_outputs": "Максимум изображений в ответе",
    "max_references": "Максимум входных изображений",
    "max_reference_mb": "Максимальный общий размер входных файлов, MiB",
    "response_formats": "Форматы ответа",
    "max_size_edge": "Максимальная сторона, пикселей",
    "request_fields": "Поля запроса",
    "output_format": "Формат файла",
}


async def documented_models(db, partner_id=None):
    """Public contracts, plus explicitly granted restricted models for this partner."""
    query = select(Model)
    if partner_id is None:
        query = query.where(Model.status == "production")
    else:
        query = query.outerjoin(
            PartnerModelGrant,
            and_(
                PartnerModelGrant.model_id == Model.id,
                PartnerModelGrant.partner_id == partner_id,
                PartnerModelGrant.revoked_at.is_(None),
            ),
        ).where(or_(Model.status == "production", and_(
            Model.status == "restricted", PartnerModelGrant.id.is_not(None),
        )))
    rows = (await db.execute(query.order_by(Model.modality, Model.name, Model.id))).scalars().all()
    return [row for row in rows if row.slug in MODELS]


@dataclass(frozen=True)
class ModelDocument:
    slug: str
    name: str
    base_url: str
    category: str
    endpoint: str
    details: tuple[str, ...]
    examples: tuple

    @property
    def guide_url(self):
        anchor = {"chat": "text", "image": "images", "video": "video"}[self.category]
        return f"{self.base_url}/docs#{anchor}"


def _display(value):
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _details(slug, entry):
    category = entry["category"]
    if category == "chat":
        profile = PUBLIC_TEXT_MODELS.get(slug, {})
        return tuple(f"{label}: {_display(profile[key])}" for key, label in TEXT_FIELDS.items() if key in profile)
    if category == "image":
        profile = entry.get("image", {})
        lines = [f"{label}: {_display(profile[key])}" for key, label in IMAGE_FIELDS.items() if key in profile]
        if "max_references" not in profile:
            lines.append(f"Лимит входных изображений в JSON на уровне API: {image_reference_limit(slug)}")
        for size in profile.get("output_sizes", profile.get("sizes", [])):
            lines.append(f"{size['aspect_ratio']}: {_display(size['pixels'])}")
        return tuple(lines)
    if slug in NATIVE_VIDEO_LIMITS:
        return (NATIVE_VIDEO_LIMITS[slug][0],)
    for name, duration, resolution, refs in VIDEO_LIMITS:
        if name == slug:
            return (
                f"Длительность результата: {duration} с",
                f"Разрешения: {resolution}",
                f"Лимиты референсов (изображения / видео / аудио / всего): {refs}",
            )
    return ()


def _examples(slug, entry):
    endpoint = entry["endpoint"].removeprefix("/v1/")
    if entry["category"] == "chat":
        prompt = "Explain a database transaction in two sentences."
        minimal = {"model": slug, "input": prompt} if endpoint == "responses" else {
            "model": slug, "messages": [{"role": "user", "content": prompt}],
        }
        if endpoint == "messages":
            minimal["max_tokens"] = 512
        candidates = [(endpoint, minimal)]
        candidates += [(ex["protocol"], ex["body"]) for ex in TEXT_EXAMPLES
                       if ex["body"].get("model") == slug and ex["protocol"] == endpoint]
    else:
        minimal = {"model": slug, "prompt": "A ceramic cup on a plain blue background"}
        if entry["category"] == "video":
            minimal["prompt"] = "A slow camera move around a ceramic cup"
            minimal["duration"] = 5
        candidates = [(endpoint, minimal)]
        candidates += [(protocol, body) for protocol, body in MEDIA_EXAMPLES.values() if body.get("model") == slug]
        candidates += [(endpoint, body) for body in NATIVE_VIDEO_EXAMPLES.get(slug, [])]
    valid = []
    for protocol, body in candidates:
        try:
            checked = validate_request(protocol, body)
        except ValueError:
            continue
        if (protocol, checked) not in valid:
            valid.append((protocol, checked))
    if not valid:
        raise ValueError("model_document_example_missing")
    return tuple(valid)


def model_document(slug: str, name: str, base_url: str) -> ModelDocument:
    entry = MODELS[slug]
    return ModelDocument(
        slug, name, base_url.rstrip("/"), entry["category"], entry["endpoint"],
        _details(slug, entry), _examples(slug, entry),
    )


def _result_instructions(doc):
    if doc.category == "video":
        return (
            "HTTP 202: сохраните request_id. Проверяйте GET /v1/videos/{request_id} каждые 10–15 секунд. "
            "При done скачайте GET /v1/videos/{request_id}/content с авторизацией. "
            "При failed или expired завершите задачу с ошибкой. Не создавайте новую генерацию при timeout."
        )
    if doc.category == "image":
        return (
            "HTTP 200: обработайте data[]; декодируйте b64_json или скачайте файл по url. "
            "Ссылки временные. Не передавайте API-ключ на внешние адреса. "
            "Сохраните X-Request-Id для восстановления ответа."
        )
    if doc.endpoint.endswith("/responses"):
        return "HTTP 200: извлеките output_text из блоков output[].content[]. Проверьте status и usage."
    if doc.endpoint.endswith("/messages"):
        return "HTTP 200: обработайте текстовые блоки content[], stop_reason и usage."
    return "HTTP 200: обработайте choices[].message.content, finish_reason и usage."


def manual_markdown(doc):
    lines = [
        f"# {doc.name}", f"Model ID: {doc.slug}", f"Категория: {CATEGORY_LABELS[doc.category]}",
        f"Базовый URL: {doc.base_url}", f"Основной метод: POST {doc.endpoint}", "",
        "## Авторизация",
        "Authorization: Bearer <PARTNER_API_KEY>",
        "Content-Type: application/json",
        "Idempotency-Key: <unique-request-key>",
        "Храните ключ на сервере. Сохраните тело запроса и уникальный ключ идемпотентности "
        "до отправки; при повторе той же операции используйте тот же ключ.", "",
        "## Параметры и ограничения",
        *(doc.details or ("Дополнительные характеристики не подтверждены. Используйте основной пример.",)),
        "", "## Примеры запросов",
    ]
    for protocol, body in doc.examples:
        lines.extend([f"POST /v1/{protocol}", "```json", json.dumps(body, ensure_ascii=False, indent=2), "```"])
    lines.extend([
        "", "## Получение результата", _result_instructions(doc), "",
        "## Ошибки и повторные запросы",
        "401/403: проверьте доступ и ключ. 402: проверьте доступный баланс. "
        "422: исправьте параметры. 429: учитывайте Retry-After. При сетевой ошибке или 5xx "
        "не меняйте Idempotency-Key автоматически. Для нового запроса создайте новый ключ.",
        "", "## Актуальные сведения",
        f"Документация: {doc.guide_url}",
        f"Каталог и тарифы: {doc.base_url}/prices",
        f"Доступность: GET {doc.base_url}/v1/models",
        "Примеры проверяются локальным контрактом API; наличие поля не гарантирует поддержку всех режимов модели.",
    ])
    return "\n".join(lines)


def agent_instructions(doc):
    protocol, body = doc.examples[0]
    details = "\n".join(f"- {line}" for line in doc.details[:12]) or "- Дополнительные параметры не подтверждены."
    return (
        f"# Задача: интегрировать {doc.slug} через API Нейроныча\n\n"
        "Изучи существующий проект и его инструкции. Реализуй интеграцию в действующей архитектуре, "
        "не переписывай несвязанный код.\n\n"
        f"Base URL: {doc.base_url}\nModel ID: {doc.slug}\nPOST /v1/{protocol}\n"
        f"Справочник: {doc.guide_url}\nТарифы: {doc.base_url}/prices\n\n"
        "## Контракт\n"
        "- Проверь доступность через GET /v1/models или доступ аккаунта к закрытой модели.\n"
        "- Храни NEIRONYCH_API_KEY в переменной окружения или secrets manager; не включай ключ в код, "
        "логи, браузер или промпт.\n"
        "- Заголовки: Authorization: Bearer <PARTNER_API_KEY>, Content-Type: application/json, "
        "Idempotency-Key (8–160 символов). Сохрани ключ и тело до отправки; не меняй их при повторе операции.\n"
        f"{details}\n\n"
        f"## Пример JSON\n```json\n{json.dumps(body, ensure_ascii=False, indent=2)}\n```\n\n"
        f"## Обработка ответа\n{_result_instructions(doc)}\n\n"
        "## Требования к реализации\n"
        "- Настраиваемые base URL и model ID, конечные таймауты, ограниченные retries с backoff, "
        "обработка 401/402/403/422/429/5xx. При 429 учитывай Retry-After.\n"
        "- После неоднозначной ошибки восстанови статус или исходный ответ с тем же ключом. "
        "Не создавай дублирующие платные задачи. Сохраняй request_id и безопасные диагностические данные.\n"
        "- Не выдумывай параметры, лимиты, цену и возможности модели; сверяй их с актуальным справочником. "
        "Не отправляй ключ на внешние URL.\n"
        "- Добавь тесты успешного ответа, ошибок, повторов и таймаута; используй mocks, "
        "платный live-тест запускай только с разрешения владельца. Покажи изменённые файлы и результаты тестов.\n"
    )


@lru_cache(maxsize=64)
def _pdf_cached(name, markdown):
    from app.api.model_pdf import build_pdf

    return build_pdf(name, markdown)


def render_model_pdf(doc):
    return _pdf_cached(doc.name, manual_markdown(doc))


async def model_download(rows, slug, base_url, output):
    selected = next((row for row in rows if row.slug == slug), None)
    if selected is None:
        raise HTTPException(404, "model_not_available")
    doc = model_document(selected.slug, selected.name, base_url)
    if output == "pdf":
        data = await asyncio.to_thread(render_model_pdf, doc)
        extension, media_type = "api.pdf", "application/pdf"
    elif output == "agent":
        data = agent_instructions(doc).encode("utf-8")
        extension, media_type = "agent.md", "text/markdown"
    else:
        raise HTTPException(400, "invalid_document_format")
    return Response(
        data, media_type=media_type, headers={
            "Content-Disposition": f'attachment; filename="{doc.slug}-{extension}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def model_link(root, slug, **extra):
    return root + "&" + urlencode({"model": slug, **extra})


def render_model_selection(rows, base_url, root, slug="", *, lang="ru"):
    selected = next((row for row in rows if row.slug == slug), None)
    if selected is None:
        parts = ['<section id="integrations"><h2>Интеграция моделей / Model integration</h2>',
                 '<p>Выберите модель, затем способ интеграции: PDF или инструкция для ИИ.</p>']
        for category, label in CATEGORY_LABELS.items():
            items = [row for row in rows if MODELS[row.slug]["category"] == category]
            if items:
                parts.append(f"<h3>{label}</h3><ul>")
                parts.extend(
                    f'<li><a href="{escape(model_link(root, row.slug), quote=True)}#integrations">{escape(row.name)}</a> '
                    f'<code>{escape(row.slug)}</code></li>' for row in items
                )
                parts.append("</ul>")
        if not rows:
            parts.append("<p>Нет доступных моделей.</p>")
        parts.append("</section>")
        return "".join(parts)
    doc = model_document(selected.slug, selected.name, base_url)
    pdf_url = escape(model_link(root, slug, format="pdf"), quote=True)
    agent_url = escape(model_link(root, slug, format="agent"), quote=True)
    return (
        f'<section id="integrations"><a href="{escape(root, quote=True)}#integrations">← Все модели</a>'
        f'<h2>{escape(doc.name)}</h2><p>Model ID: <code>{escape(doc.slug)}</code></p>'
        '<h3>Ручная интеграция</h3><p>API-контракт, примеры запросов и правила обработки ответов.</p>'
        f'<p><a class="button button-secondary" href="{pdf_url}">Скачать PDF</a></p>'
        '<h3>Интеграция через ИИ</h3><p>Скопируйте инструкцию в своего агента или скачайте Markdown.</p>'
        '<p><button class="copy-button" type="button" data-copy-target="model-agent-instructions">'
        'Скопировать инструкцию для ИИ</button> '
        f'<a class="button button-secondary" href="{agent_url}">Скачать .md</a></p>'
        f'<pre><code id="model-agent-instructions">{escape(agent_instructions(doc))}</code></pre></section>'
    )
