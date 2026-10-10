"""Curated partner inference reference; never serialize the private catalog here."""

import json
from html import escape
from importlib.resources import files

from app.api.text_reference import render_text_reference

# These exact bodies are rendered and exercised against the request validator.
EXAMPLES = {
    "image": (
        "images/generations",
        {
            "model": "gpt-image-2",
            "prompt": "A ceramic cup on a plain blue background",
            "n": 1,
            "size": "1024x1024",
            "response_format": "b64_json",
        },
    ),
    "image-edit": (
        "images/edits",
        {
            "model": "gpt-image-2",
            "prompt": "Change the background to blue",
            "images": [{"image_url": "https://media.example.com/cup.png"}],
            "n": 1,
            "response_format": "b64_json",
        },
    ),
    "image-nano-2": (
        "images/generations",
        {
            "model": "nano-banana-2",
            "prompt": "A cinematic mountain panorama",
            "resolution": "4k",
            "aspect_ratio": "16:9",
            "n": 1,
            "response_format": "b64_json",
        },
    ),
    "image-nano-pro": (
        "images/generations",
        {
            "model": "nano-banana-pro",
            "prompt": "A cinematic mountain panorama",
            "resolution": "4k",
            "aspect_ratio": "21:9",
            "n": 1,
            "response_format": "b64_json",
        },
    ),
    "image-sunburst": (
        "images/generations",
        {
            "model": "gpt-image-2.5-sunburst",
            "prompt": "A cinematic mountain panorama",
            "size": "3840x2160",
            "quality": "high",
            "n": 1,
            "response_format": "b64_json",
        },
    ),
    "video-reference": (
        "videos/generations",
        {
            "model": "seedance-2.5",
            "prompt": "Animate the image in @Image 1 with a gentle camera move.",
            "reference_images": [{"url": "https://media.example.com/reference.jpg"}],
            "duration": 4,
            "resolution": "480p",
            "aspect_ratio": "9:16",
        },
    ),
    "video-frames": (
        "videos/generations",
        {
            "model": "seedance-2.5",
            "prompt": "A smooth camera move between the frames",
            "start_image": {"url": "https://media.example.com/start.jpg"},
            "end_image": {"url": "https://media.example.com/end.jpg"},
            "duration": 4,
            "resolution": "720p",
            "aspect_ratio": "adaptive",
        },
    ),
    "video-edit": (
        "videos/generations",
        {
            "model": "seedance-2.5",
            "prompt": "Replace the sky in @Video 1 with a sunset",
            "reference_videos": [{"url": "https://media.example.com/source.mp4"}],
            "omni_reference_task_type": "edit",
            "duration": -1,
            "resolution": "720p",
            "aspect_ratio": "adaptive",
        },
    ),
}

# Public contract limits, deliberately independent of procurement/pricing metadata.
VIDEO_LIMITS = (
    ("seedance-2.5", "4–30", "480p, 720p, 1080p", "30 / 10 / 10 / 50"),
    ("seedance-2.5-self-developed-nsfw", "4–30", "720p, 1080p", "30 / 10 / 10 / 50"),
    ("seedance-2.0", "4–15", "480p, 720p, 1080p, 4k", "9 / 3 / 3 / 12"),
    ("seedance-2.0-self-developed-nsfw", "4–15", "720p, 1080p, 4k", "9 / 3 / 3 / 12"),
    ("seedance-2.0-mini", "4–15", "480p, 720p", "9 / 3 / 3 / 12"),
    ("seedance-2.0-fast", "4–15", "480p, 720p", "9 / 3 / 3 / 12"),
    ("minimax-h3", "4–15", "768p, 2k", "9 / 3 / 3 / 15"),
    ("wan-3", "2–30", "480p, 720p, 1080p", "10 / 5 / 5 / 20"),
    ("grok-imagine-video-1.5", "1–15", "480p, 720p, 1080p", "7 / 0 / 0 / 7"),
)


def code(value):
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, indent=2)
    return "<pre><code>" + escape(value) + "</code></pre>"


def table(headers, rows):
    return (
        '<div class="table-scroll"><table><thead><tr>'
        + "".join("<th>" + escape(str(h)) + "</th>" for h in headers)
        + "</tr></thead><tbody>"
        + "".join("<tr>" + "".join("<td>" + escape(str(c)) + "</td>" for c in row) + "</tr>" for row in rows)
        + "</tbody></table></div>"
    )


def render_reference(base_url: str, lang: str, *, video_models: set[str] | None = None) -> str:
    def t(ru, en):
        return ru if lang == "ru" else en

    def p(ru, en):
        return "<p>" + escape(t(ru, en)) + "</p>"

    def heading(anchor, ru, en):
        return f'<h2 id="{anchor}">{escape(t(ru, en))}</h2>'

    def params(rows):
        return table(
            (t("Параметр", "Parameter"), t("Тип / обязательность", "Type / required"), t("Правила", "Rules")), rows
        )

    def example(name):
        protocol, body = EXAMPLES[name]
        return code(f"POST /v1/{protocol}") + code(body)

    body = p(
        "Нейроныч API · Контракт проверен 28.09.2026. Здесь описаны запросы генерации, их параметры и ответы. "
        "Ключ выдаётся после подключения партнёра. Для генерации нужны активный ключ, доступная модель и баланс.",
        "Neironych API · Contract reviewed 2026-09-28. This reference covers generation requests, parameters and "
        "responses. Obtain a key through partner onboarding. Generations require an active key, an enabled model "
        "and sufficient balance.",
    )
    anchors = [
        ("connect", "Подключение", "Connection"),
        ("models", "Модели", "Models"),
        ("text", "Текст", "Text"),
        ("images", "Изображения", "Images"),
        ("video", "Видео", "Video"),
        ("uploads", "Загрузка референса", "Reference upload"),
        ("python", "Полный пример Python", "Complete Python example"),
        ("errors", "Ошибки и повторы", "Errors and repeated requests"),
    ]
    body += (
        '<nav class="contents">'
        + " · ".join(f'<a href="#{anchor}">{escape(t(ru, en))}</a>' for anchor, ru, en in anchors)
        + "</nav>"
    )
    body += heading("connect", "Подключение", "Connection") + code(base_url)
    body += code(
        "Authorization: Bearer <PARTNER_API_KEY>\nContent-Type: application/json\n"
        "Idempotency-Key: <unique-request-key>\nX-Client-Request-Id: <your-task-id>"
    )
    body += p(
        "Base URL выше не содержит /v1. Указывайте полный путь метода. Все операции, кроме GET /v1/models, "
        "требуют авторизацию. Вместо Authorization допустим x-api-key; не передавайте оба заголовка. "
        "Ключ храните на своём сервере, не в браузере или мобильном клиенте.",
        "The base URL above does not include /v1. Append the complete method path. Every operation except "
        "GET /v1/models requires authentication. x-api-key is an alternative to Authorization; do not send both. "
        "Keep the key on your server, never in a browser or mobile client.",
    )
    body += p(
        "Idempotency-Key обязателен для всех POST генерации текста, изображений и видео: строка 8–160 символов, "
        "например UUID. Один ключ обозначает один запрос в пределах партнёра, включая все его API-ключи. "
        "Сохраните ключ и исходное тело до отправки. Для получения статуса, скачивания и создания "
        "загрузки он не нужен.",
        "Idempotency-Key is required for every text, image and video generation POST: a string of 8–160 characters, "
        "such as a UUID. A key identifies one request across the partner account, including its other API keys. "
        "Persist the key and original body before submitting. Status, download and upload-ticket "
        "operations do not need it.",
    )
    body += p(
        "Необязательный X-Client-Request-Id сохраняет UUID задачи вашей системы и возвращается в статусе и callback. "
        "Если ключ имеет вид generation:<UUID>:provider:<N>, UUID извлекается автоматически. Потерянный ответ "
        "можно восстановить через GET /api/v1/generations/by-client-request-id/<UUID>.",
        "Optional X-Client-Request-Id persists your system task UUID and returns it in status and callback payloads. "
        "For generation:<UUID>:provider:<N> keys, the UUID is extracted automatically. Recover a lost response with "
        "GET /api/v1/generations/by-client-request-id/<UUID>.",
    )
    body += code(
        f'export API_BASE="{base_url}"\nexport API_KEY="<PARTNER_API_KEY>"\ncurl --fail-with-body "$API_BASE/v1/models"'
    )
    body += table(
        (t("Метод", "Method"), t("Успешный ответ", "Success response")),
        [
            ("GET /v1/models", "200 application/json"),
            ("POST /v1/responses", "200 application/json | text/event-stream"),
            ("POST /v1/chat/completions", "200 application/json | text/event-stream"),
            ("POST /v1/messages", "200 application/json | text/event-stream"),
            ("POST /v1/images/generations", "200 application/json"),
            ("POST /v1/images/edits", "200 application/json"),
            ("POST /v1/videos/generations", "202 application/json"),
            ("GET /v1/videos/{request_id}", "200 application/json"),
            ("GET /v1/videos/{request_id}/content", "200 video/mp4 | 206 partial content"),
            ("GET /api/v1/generations/{request_id}/trace", "200 application/json"),
            ("POST /v1/media/uploads", "201 application/json"),
        ],
    )
    # The enabled-model table is inserted by guide.py, matching GET /v1/models.
    body += heading("models", "Доступность моделей", "Model availability")
    body += p(
        "GET /v1/models — актуальный список включённых Model ID, без пагинации и без параметров. "
        "data может быть пустым. Наличие модели в примере или таблице ограничений ниже не означает, что она включена "
        "для генераций. Отдельные режимы и размеры также должны быть доступны для вашего подключения. "
        "Model ID передавайте точно, включая регистр и версию.",
        "GET /v1/models returns currently enabled model IDs, with no pagination or parameters. data may be empty. "
        "A model appearing in an example or limits table below does not mean it is enabled for generations. "
        "Individual modes and sizes must also be available for your connection. Send the exact model ID, "
        "including its case and version.",
    )
    body += code({"object": "list", "data": [{"id": "seedance-2.5", "object": "model", "owned_by": "neironych"}]})
    body += "<!-- enabled-models -->"
    body += render_text_reference(lang)
    body += heading("images", "Изображения", "Images")
    body += p(
        "POST /v1/images/generations создаёт изображения. POST /v1/images/edits принимает JSON с images "
        "или multipart/form-data с файлами. Запрос синхронный: дождитесь ответа, не создавайте второй при задержке.",
        "POST /v1/images/generations creates images. POST /v1/images/edits accepts JSON with images or "
        "multipart/form-data with files. Requests are synchronous: wait for the response; a delay is not a reason "
        "to submit another generation.",
    )
    body += params(
        [
            (
                "model",
                "string · required",
                t("Model ID семейства изображений из /v1/models.", "Image model ID from /v1/models."),
            ),
            (
                "prompt",
                "string · required",
                t("Непустое описание результата/изменений.", "Non-empty output/edit description."),
            ),
            ("n", "integer · optional, 1", "GPT Image: 1–7; Nano Banana: 1–4; Grok Image: 1–10."),
            (
                "images",
                "array<object> · edits",
                t(
                    'JSON: [{"image_url":"https://…"}] или image_url с data:image/…;base64,…; '
                    "GPT до 16, Nano Banana Pro до 14, остальные до 3.",
                    'JSON: [{"image_url":"https://…"}] or a data:image/…;base64,… image_url; '
                    "GPT up to 16, Nano Banana Pro up to 14, others up to 3.",
                ),
            ),
            (
                "mask",
                "object | file · optional",
                t(
                    'GPT edits: {"image_url":"https://…/mask.png"} или файл PNG с альфа-каналом; '
                    "прозрачная область меняется. "
                    "Размер совпадает с первым изображением; не более одной маски.",
                    'GPT edits: {"image_url":"https://…/mask.png"} or a PNG file with alpha; transparent '
                    "areas are edited. "
                    "Match the first image dimensions; at most one mask.",
                ),
            ),
            (
                "size",
                "string · optional, auto",
                t(
                    "GPT: auto или WIDTHxHEIGHT, например 1024x1024. Обе стороны положительные. "
                    "Итоговые пиксели могут отличаться от запроса; проверяйте результат.",
                    "GPT: auto or WIDTHxHEIGHT, e.g. 1024x1024. Both dimensions are positive. "
                    "Actual output pixels may differ; inspect the result.",
                ),
            ),
            ("quality", "string · optional, auto", "GPT Image: auto | low | medium | high."),
            (
                "resolution",
                "string · optional",
                t(
                    "Nano Banana 2/Pro: 1k (по умолчанию), 2k или 4k. Grok: 1k или 2k; "
                    "доступность зависит от модели/режима. Nano Banana 2 Lite поддерживает только 1k. "
                    "GPT, включая Sunburst: размер задаётся через size, например 3840x2160 для 4K.",
                    "Nano Banana 2/Pro: 1k (default), 2k or 4k. Grok: 1k or 2k; availability depends "
                    "on model/mode. Nano Banana 2 Lite supports only 1k. GPT, including Sunburst: set dimensions "
                    "using size, e.g. 3840x2160 for 4K.",
                ),
            ),
            (
                "aspect_ratio",
                "string · optional",
                t(
                    "Nano: 1:1 (по умолчанию), 16:9, 9:16, 4:3, 3:4. Nano Banana Pro также принимает "
                    "3:2, 2:3, 5:4, 4:5, 21:9; при edits без aspect_ratio результат тоже 1:1, "
                    "соотношение референса не наследуется. У GPT желаемую композицию также задавайте в prompt.",
                    "Nano: 1:1 (default), 16:9, 9:16, 4:3, 3:4. Nano Banana Pro also accepts 3:2, 2:3, "
                    "5:4, 4:5, 21:9; edits without aspect_ratio also default to 1:1 rather than following "
                    "the reference frame. For GPT, also describe the desired composition in prompt.",
                ),
            ),
            (
                "response_format",
                "string · optional",
                t(
                    "b64_json или url для GPT/Grok; Nano поддерживает только b64_json. "
                    "Nano Banana Pro возвращает JPEG в data[].b64_json. "
                    "Для воспроизводимости задавайте формат явно.",
                    "b64_json or url for GPT/Grok; Nano supports only b64_json. Nano Banana Pro returns "
                    "JPEG in data[].b64_json. Set the format explicitly for predictable output.",
                ),
            ),
        ]
    )
    body += example("image") + example("image-edit")
    body += "<h3>Nano Banana 2 · 4K</h3>" + example("image-nano-2")
    body += "<h3>Nano Banana Pro · 4K</h3>" + example("image-nano-pro")
    body += "<h3>GPT Image 2.5 Sunburst · 4K</h3>" + example("image-sunburst")
    body += p(
        "В multipart передавайте model, prompt, n как поля формы; файлы — повторяющимся image[] "
        "(также принимаются image, images, images[]). Лимиты файлов: GPT 16, Nano Banana Pro 14, "
        "другие Nano 3, Grok 1; "
        "плюс одна mask у GPT. Не задавайте Content-Type вручную: клиент добавит boundary.",
        "For multipart, send model, prompt and n as form fields; repeat image[] for files "
        "(image, images and images[] are also accepted). File limits: GPT 16, Nano Banana Pro 14, "
        "other Nano 3, Grok 1; "
        "plus one GPT mask. Do not set Content-Type manually: the client supplies the boundary.",
    )
    body += code(
        'curl --fail-with-body "$API_BASE/v1/images/edits" \\\n'
        '  -H "Authorization: Bearer $API_KEY" \\\n'
        '  -H "Idempotency-Key: $REQUEST_KEY" \\\n'
        '  -F "model=gpt-image-2" -F "prompt=Change the background to blue" \\\n'
        '  -F "n=1" -F "response_format=b64_json" -F "image[]=@reference.png"'
    )
    body += code({"created": 1790596800, "data": [{"b64_json": "<base64-encoded-image>"}]})
    body += p(
        "Ответ содержит data[] с b64_json или url в выбранном формате, возможен revised_prompt. "
        "Декодируйте base64 либо сразу скачайте URL. Ссылки временные; срок постоянного хранения не гарантирован. "
        "На внешние ссылки изображений не отправляйте API-ключ. X-Request-Id содержит наш ID запроса. Потерянный "
        "HTTP-ответ можно восстановить через статус по нашему ID или X-Client-Request-Id. "
        "У GPT размерный класс определяется фактическими пикселями: до 1024 по длинной стороне — 1K, "
        "до 2048 — 2K, выше — 4K. Учитывается каждое возвращённое изображение.",
        "The response contains data[] with b64_json or url in the chosen format, and may include revised_prompt. "
        "Decode base64 or download the URL promptly. Links are temporary; permanent retention is not guaranteed. "
        "Never send your API key to external image URLs. X-Request-Id contains our request ID. Recover a lost "
        "HTTP response through status lookup by our ID or X-Client-Request-Id. "
        "GPT size tiers use actual pixels: longest edge up to 1024 is 1K, up to 2048 is 2K, above is 4K. "
        "Each returned image counts.",
    )
    body += heading("video", "Видео: создание и получение", "Video: creation and retrieval")
    body += p(
        "POST /v1/videos/generations принимает только JSON. Все модели возвращают 202 с request_id; "
        "это подтверждение приёма запроса, а не готовности видео. Сохраните ID. Все значения duration — целые секунды.",
        "POST /v1/videos/generations accepts JSON only. Every model returns 202 with request_id; "
        "this acknowledges the request, not a completed video. Save the ID. duration values are integer seconds.",
    )
    body += params(
        [
            ("model", "string · required", t("Точный ID доступной видеомодели.", "Exact enabled video model ID.")),
            (
                "prompt",
                "string",
                t(
                    "Обязателен без входных медиа; всегда обязателен для edit, Wan и MiniMax. "
                    "Seedance: до 40 000 байт UTF-8; Wan: до 4500 символов. Ссылки на референсы: @Image "
                    "1, @Video 1, @Audio 1.",
                    "Required without input media; always required for edit, Wan and MiniMax. "
                    "Seedance: up to 40,000 UTF-8 bytes; Wan: up to 4,500 characters. Refer to inputs as "
                    "@Image 1, @Video 1, @Audio 1.",
                ),
            ),
            (
                "duration",
                "integer",
                t(
                    "Задавайте явно в диапазоне своей модели, см. таблицу. Для Seedance/MiniMax/Wan при пропуске — 5; "
                    "у Grok задавайте обязательно для предсказуемой длины. Для Seedance 2.5 edit — -1 или пропустить.",
                    "Set explicitly within the model range below. Seedance/MiniMax/Wan default to 5 when omitted; "
                    "always set it for Grok to make length predictable. Seedance 2.5 edit: -1 or omit.",
                ),
            ),
            (
                "resolution",
                "string · optional",
                t(
                    "По умолчанию 720p, у MiniMax 768p. Допустимые значения в таблице ниже, регистр значим.",
                    "Default 720p, or 768p for MiniMax. Allowed case-sensitive values are listed below.",
                ),
            ),
            (
                "aspect_ratio",
                "string · optional",
                t(
                    "Задавайте явно для текстового и референсного режима. Базовые: 1:1, 16:9, 9:16, 4:3, 3:4. "
                    "Seedance/MiniMax также 21:9; Grok также 3:2, 2:3. Правила adaptive ниже.",
                    "Set explicitly for text and reference modes. Base values: 1:1, 16:9, 9:16, 4:3, 3:4. "
                    "Seedance/MiniMax also support 21:9; Grok also supports 3:2, 2:3. See adaptive rules below.",
                ),
            ),
            ("n", "integer · optional, 1", t("Только 1.", "Only 1.")),
            (
                "reference_images / reference_videos / reference_audios",
                "array<object> · optional",
                t(
                    '[{"url":"https://…"}]. Порядок определяет нумерацию референсов. Лимиты каждого типа и суммы ниже. '
                    "Не передавайте duration референса: длина определяется по файлу.",
                    '[{"url":"https://…"}]. Order determines reference numbering. Per-type and combined limits below. '
                    "Do not supply a reference duration: it is determined from the file.",
                ),
            ),
            (
                "start_image / end_image",
                "object · optional",
                t(
                    'Seedance/MiniMax/Wan: {"url":"https://…"}. end_image требует start_image. '
                    "Кадры нельзя смешивать с reference_*.",
                    'Seedance/MiniMax/Wan: {"url":"https://…"}. end_image requires start_image. '
                    "Frames cannot be combined with reference_*.",
                ),
            ),
            (
                "image",
                "object · optional",
                t(
                    'Grok: первый кадр {"url":"https://…"}; не совмещайте с reference_images.',
                    'Grok: first frame {"url":"https://…"}; do not combine with reference_images.',
                ),
            ),
            (
                "omni_reference_task_type",
                "string · optional, auto",
                t(
                    "Seedance 2.5: auto, reference или edit; для edit см. отдельный пример.",
                    "Seedance 2.5: auto, reference or edit; see the dedicated edit example.",
                ),
            ),
            (
                "generate_audio",
                "boolean · optional",
                t(
                    "Seedance 2.5: true создаёт звук, false — видео без звука; только boolean, не строка. "
                    "Wan также поддерживает переключатель. Контракты остальных моделей не меняются.",
                    "Seedance 2.5: true generates audio, false returns a silent video; use a boolean, not a string. "
                    "Wan also supports an audio toggle. Other model contracts are unchanged.",
                ),
            ),
            (
                "seed / watermark",
                "model-dependent",
                t(
                    "Seedance/MiniMax: seed запрещён; watermark=true запрещён. Отсутствие ошибки формата для других "
                    "моделей не подтверждает поддержку такой настройки.",
                    "Seedance/MiniMax: seed is unsupported; watermark=true is rejected. Passing format validation for "
                    "another model does not establish support for an option.",
                ),
            ),
        ]
    )
    body += table(
        (
            "Model ID",
            t("Секунды", "Seconds"),
            "resolution",
            t("Фото / видео / аудио / всего", "Images / videos / audio / total"),
        ),
        (row for row in VIDEO_LIMITS if video_models is None or row[0] in video_models),
    )
    body += p(
        "Self-Developed NSFW: используйте точный Model ID из таблицы. Правила кадров, референсов и аудио "
        "соответствуют семейству Seedance 2.0 или 2.5; версия 2.5 также поддерживает edit. "
        "Варианты Self-Developed NSFW не принимают 480p: 2.0 принимает 720p, 1080p и 4k, "
        "2.5 — 720p и 1080p. Пример 480p ниже относится к обычной seedance-2.5. "
        "Текущую доступность проверяйте через GET /v1/models.",
        "Self-Developed NSFW: use the exact Model ID in the table. Frame, reference and audio rules follow "
        "the Seedance 2.0 or 2.5 family; version 2.5 also supports edit. "
        "Self-Developed NSFW variants reject 480p: 2.0 supports 720p, 1080p and 4k; "
        "2.5 supports 720p and 1080p. The 480p example below applies to ordinary seedance-2.5. "
        "Check GET /v1/models for current availability.",
    )
    body += p(
        "Seedance 2.5: текст и обычные референсы используют фиксированный aspect_ratio; adaptive здесь запрещён. "
        "Для start_image/end_image — только adaptive либо отсутствие aspect_ratio. Seedance 2.0 с кадрами "
        "также принимает фиксированные пропорции. MiniMax с кадрами: aspect_ratio не передавайте — он берётся "
        "из изображения; с референсами можно adaptive. Grok с reference_images допускает только 480p/720p. "
        "Seedance 2.0 и MiniMax требуют изображение/видео вместе с аудио; Seedance 2.5 допускает только аудио.",
        "Seedance 2.5 text and ordinary references use a fixed aspect_ratio; adaptive is rejected in these modes. "
        "With start_image/end_image, use adaptive or omit aspect_ratio. Seedance 2.0 frames also accept fixed ratios. "
        "MiniMax frames: omit aspect_ratio, which is derived from the image; reference mode also accepts adaptive. "
        "Grok reference_images support only 480p/720p. Seedance 2.0 and MiniMax require image/video alongside audio; "
        "Seedance 2.5 allows audio alone.",
    )
    body += p(
        "Wan: при входных кадрах не задавайте aspect_ratio. Суммарная длительность reference_videos не более "
        "15 секунд, а результат плюс входные видео — не более 30 секунд. Например, для 15 секунд референса "
        "выход ограничен 15 секундами. Проверка формата запроса не заменяет проверку содержимого медиа.",
        "Wan: omit aspect_ratio with input frames. Total reference_videos duration is at most 15 seconds, "
        "and output plus input videos must not exceed 30 seconds. For example, 15 seconds of reference video "
        "leaves at most 15 seconds for output. Request format validation does not replace media validation.",
    )
    body += "<h3>Seedance 2.5 · " + t("референс", "reference") + "</h3>" + example("video-reference")
    body += p(
        "Сохраните JSON как request.json. Создайте REQUEST_KEY один раз и сохраните его вместе с этим файлом; "
        "при повторе той же операции не генерируйте новый ключ.",
        "Save the JSON as request.json. Create REQUEST_KEY once and preserve it with the file; "
        "do not generate a new key when repeating the same operation.",
    )
    body += code(
        "REQUEST_KEY=\"$(python3 -c 'import uuid; print(uuid.uuid4())')\"\n"
        'curl --fail-with-body "$API_BASE/v1/videos/generations" \\\n'
        '  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \\\n'
        '  -H "Idempotency-Key: $REQUEST_KEY" --data-binary @request.json'
    )
    body += "<h3>Seedance 2.5 · " + t("первый и последний кадры", "first and last frames") + "</h3>"
    body += example("video-frames")
    body += "<h3>Seedance 2.5 · edit</h3>" + example("video-edit")
    body += p(
        "Тарификация Seedance 2.5: если в запросе есть reference_videos, применяется тариф edit "
        "для выбранного разрешения, даже когда omni_reference_task_type=reference и также переданы фото/аудио. "
        "Без reference_videos применяется обычный тариф. "
        "При наличии видео поставщик считает оплачиваемые секунды как длительность результата плюс суммарную "
        "длительность видеореференсов; после завершения списывается только фактический usage. "
        "Наличие видео не меняет режим запроса автоматически.",
        "Seedance 2.5 billing: when reference_videos are present, the edit retail rate applies at the selected "
        "resolution, even for reference mode combined with image/audio. Without reference_videos, the normal rate "
        "applies. With video input, billable seconds include output plus reference video duration; final billing "
        "uses actual provider usage. Pricing never changes the provider request mode.",
    )

    body += p(
        "Для seedance-2.5 edit обязателен prompt и ровно один reference_videos, длиной 4–30 секунд. "
        "Фото, аудио и второе видео в edit не принимаются. Для смешанных референсов используйте reference "
        "с явно выбранной длительностью: сервер не переключает режим и не удаляет референсы автоматически. "
        "В edit длина и пропорции следуют исходному ролику: duration=-1 или пропустить, "
        "aspect_ratio=adaptive или пропустить; start_image, end_image и фиксированный size не используются.",
        "seedance-2.5 edit requires prompt and exactly one reference_videos entry, 4–30 seconds long. "
        "Images, audio and a second video are not accepted in edit. Use reference with an explicit duration "
        "for mixed assets: the server never silently changes modes or removes references. "
        "Edit output duration and aspect follow that video: duration=-1 or omit, "
        "aspect_ratio=adaptive or omit; do not use start_image, end_image or fixed size.",
    )
    body += "<h3>" + t("Альтернативные имена полей", "Field aliases") + "</h3>"
    body += p(
        "Следующие формы записи описаны для Seedance, MiniMax и Wan; для Grok используйте его основные поля.",
        "The following aliases apply to Seedance, MiniMax and Wan; use the primary Grok fields for Grok.",
    )
    body += table(
        (t("Поле", "Field"), t("Эквивалент", "Equivalent")),
        [
            ("seconds", "duration"),
            ("ratio", "aspect_ratio"),
            ('image_urls: ["https://…"]', 'reference_images: [{"url":"https://…"}]'),
            ('video_urls: ["https://…"]', 'reference_videos: [{"url":"https://…"}]'),
            ('audio_urls: ["https://…"]', 'reference_audios: [{"url":"https://…"}]'),
            ('image_url: "https://…"', 'start_image: {"url":"https://…"}'),
            ('end_image_url: "https://…"', 'end_image: {"url":"https://…"}'),
            (
                'frame_images: [{"frame_type":"first_frame","url":"https://…"}, '
                '{"frame_type":"last_frame","url":"https://…"}]',
                "start_image + end_image",
            ),
            (
                'input_references: [{"type":"image_url","image_url":{"url":"https://…"}}]',
                "reference_images; type=video_url/audio_url → reference_videos/reference_audios",
            ),
        ],
    )
    body += p(
        "Выбирайте один способ записи: не дублируйте исходное поле и его псевдоним. Вместо resolution/aspect_ratio "
        "есть size=WIDTHxHEIGHT: соотношение должно соответствовать 1:1, 16:9, 9:16, 4:3, 3:4 или 21:9 "
        "с отклонением не более 3%. Короткая сторона <720 → 480p, <1080 → 720p, <2160 → 1080p, иначе 4k. "
        "Явные resolution/ratio не должны противоречить size. Для MiniMax 1366x768 соответствует 768p; "
        "при другом size обязателен resolution. Для новых интеграций используйте основные поля выше.",
        "Choose one notation: do not duplicate a field and its alias. size=WIDTHxHEIGHT is an alternative to "
        "resolution/aspect_ratio: ratio must match 1:1, 16:9, 9:16, 4:3, 3:4 or 21:9 within 3%. "
        "Shortest edge <720 maps to 480p, <1080 to 720p, <2160 to 1080p, otherwise 4k. Explicit resolution/ratio "
        "must agree with size. MiniMax maps 1366x768 to 768p; other sizes require an explicit resolution. "
        "Prefer the primary fields above for new integrations.",
    )
    body += "<h3>" + t("Ответ и опрос статуса", "Response and status polling") + "</h3>"
    body += code({"request_id": "<our-request-id>", "client_request_id": "<your-task-id>"})
    body += code('curl --fail-with-body "$API_BASE/v1/videos/$REQUEST_ID" \\\n  -H "Authorization: Bearer $API_KEY"')
    body += table(
        ("status", t("Значение", "Meaning")),
        [
            (
                "pending",
                t(
                    "В очереди или выполняется; продолжайте опрос раз в 10–15 секунд.",
                    "Queued or running; continue polling every 10–15 seconds.",
                ),
            ),
            (
                "done",
                t(
                    "Готово: video.url временно доступен без авторизации; authenticated_url требует ключ.",
                    "Complete: video.url is temporarily available without auth; authenticated_url requires the key.",
                ),
            ),
            (
                "failed",
                t("Терминальная ошибка; смотрите error, если он есть.", "Terminal failure; inspect error if present."),
            ),
            (
                "expired",
                t(
                    "Истекло время выполнения задачи; терминальный статус.",
                    "Task execution timed out; terminal status.",
                ),
            ),
        ],
    )
    body += code(
        {
            "request_id": "<our-request-id>",
            "client_request_id": "<your-task-id>",
            "status": "done",
            "video": {
                "url": base_url + "/api/v1/media/results/<request-id>/<expires>/<signature>",
                "expires_at": 1791201600,
                "authenticated_url": base_url + "/v1/videos/<our-request-id>/content",
            },
            "usage": {"billed_seconds": 4},
        }
    )
    body += p(
        "request_id в пути — ID из ответа 202, он доступен только своему партнёру. video появляется при done; "
        "usage.billed_seconds может отсутствовать и означает учтённые секунды, включая входное видео, "
        "а не только длительность результата. progress, ETA и video.duration не входят в ответ этого API. "
        "Не запускайте новую генерацию из-за истечения времени ожидания в своём приложении.",
        "The request_id path value comes from the 202 response and is accessible only to its partner. "
        "video appears when done; usage.billed_seconds is optional and includes chargeable input-video time, "
        "not just output duration. This API does not return progress, ETA or video.duration. "
        "Do not create a new generation because your application's waiting period elapsed.",
    )
    body += code(
        'curl --fail-with-body "$API_BASE/v1/videos/$REQUEST_ID/content" \\\n'
        '  -H "Authorization: Bearer $API_KEY" --output result.mp4'
    )
    body += p(
        "Постоянный authenticated_url требует тот же аккаунт партнёра; временный video.url можно передать "
        "Telegram или клиенту без раскрытия API-ключа. Поддерживается Range: bytes=0-1023 с ответом 206 и "
        "Content-Range при частичной отдаче. До готовности файл недоступен. Сохраните готовый MP4 у себя: "
        "постоянное хранение результата не гарантируется.",
        "The stable authenticated_url requires the same partner account; temporary video.url can be sent to "
        "Telegram or a customer without exposing the API key. Range: bytes=0-1023 is supported, with 206 and "
        "Content-Range for partial responses. The file is unavailable before completion. Save the MP4 yourself: "
        "permanent retention is not guaranteed.",
    )
    body += render_uploads(lang)
    body += heading(
        "python", "Полный пример: локальное фото → Seedance → MP4", "Complete example: local photo → Seedance → MP4"
    )
    body += p(
        "Установите Python 3.12+ и httpx (pip install httpx), сохраните код ниже как seedance_reference.py. "
        "Задайте API_BASE, API_KEY и REFERENCE_FILE — путь к JPEG/PNG. Seedance 2.5 и выбранный размер должны "
        "быть включены. При остановке запустите снова с тем же STATE_FILE: он сохраняет ключ, тело и ID запроса. "
        "Для новой платной генерации укажите другое имя STATE_FILE. Не запускайте два процесса с одним "
        "файлом состояния.",
        "Install Python 3.12+ and httpx (pip install httpx), save the code below as seedance_reference.py. "
        "Set API_BASE, API_KEY and REFERENCE_FILE to a JPEG/PNG path. Seedance 2.5 and the selected resolution "
        "must be enabled. After interruption, run again with the same STATE_FILE: it preserves the request key, "
        "body and ID. Use a different STATE_FILE for a new paid generation. Do not run two processes with "
        "one state file.",
    )
    body += code(
        'export REFERENCE_FILE="/path/to/reference.jpg"\n'
        'export STATE_FILE="seedance-request.json"\npython seedance_reference.py'
    )
    script = files("app.api.examples").joinpath("seedance_reference.py").read_text()
    body += "<details><summary>seedance_reference.py</summary>" + code(script) + "</details>"
    body += render_errors(lang)
    return body


def render_uploads(lang):
    ru = lang == "ru"
    body = '<h2 id="uploads">' + ("Загрузка референсов" if ru else "Uploading reference media") + "</h2>"
    body += (
        "<p>"
        + (
            "Для локального файла сначала создайте загрузку, затем отправьте байты по upload_url. "
            "media_url вставьте в запрос генерации. Генерация и загрузка — разные операции. "
            "POST /v1/media/uploads принимает только JSON с описанием файла, а не сам файл. "
            "Не отправляйте сюда multipart/form-data, байты JPEG/PNG или base64 вместо JSON. "
            "Такой Content-Type возвращает 415 media_upload_requires_json; повреждённый JSON — "
            "422 invalid_request_contract."
            if ru
            else "For a local file, create an upload ticket, then send bytes to upload_url. "
            "Use media_url in the generation request. Upload and generation are separate operations. "
            "POST /v1/media/uploads accepts JSON file metadata, not the file itself. "
            "Do not send multipart/form-data, raw JPEG/PNG bytes or base64 instead of JSON. "
            "An unsupported Content-Type returns 415 media_upload_requires_json; malformed JSON returns "
            "422 invalid_request_contract."
        )
        + "</p>"
    )
    body += code(
        "POST /v1/media/uploads\nContent-Type: application/json\nAuthorization: Bearer <PARTNER_API_KEY>"
    ) + code({"model": "seedance-2.5", "type": "image", "content_type": "image/jpeg", "size_bytes": 123456})
    body += table(
        ("Field", "Type", "Required"),
        [
            ("model", "string · Model ID", "yes"),
            ("type", "image | video | audio", "yes"),
            ("content_type", "string · MIME", "yes"),
            ("size_bytes", "integer · actual file bytes > 0", "yes"),
        ],
    )
    body += code(
        {
            "upload_url": "https://storage.example.com/signed-put-url",
            "media_url": "https://storage.example.com/signed-read-url",
            "upload_expires_at": "2026-09-28T12:15:00Z",
            "expires_at": "2026-10-05T12:00:00Z",
        }
    )
    body += (
        "<p>"
        + (
            "201 возвращает подписанные ссылки и сроки в UTC. PUT должен завершиться до upload_expires_at "
            "(обычно 15 минут), media_url действует до expires_at (обычно 7 дней). Ориентируйтесь на ответ. "
            "Отправляйте исходные байты, точные Content-Type и Content-Length; не JSON и не multipart. "
            "upload_url уже содержит разрешение: API-ключ на адрес хранилища не передавайте. Не меняйте "
            "query-параметры "
            "подписанных ссылок. Пример size_bytes=123456 нужно заменить реальным размером файла."
            if ru
            else "201 returns signed URLs and UTC expiration timestamps. Complete PUT before upload_expires_at "
            "(normally 15 minutes); media_url works until expires_at (normally 7 days). Use the returned timestamps. "
            "Send raw bytes and exact Content-Type and Content-Length; not JSON or multipart. "
            "upload_url already contains authorization: never send your API key to storage. Preserve signed URL "
            "query parameters. Replace the example size_bytes=123456 with the actual byte length."
        )
        + "</p>"
    )
    body += code(
        'curl --fail-with-body -X PUT "$UPLOAD_URL" \\\n  -H "Content-Type: image/jpeg" --upload-file reference.jpg'
    )
    body += table(
        ("type", "content_type", "Upload limit"),
        [
            ("image", "image/jpeg, image/png, image/webp", "20 MiB"),
            ("video", "video/mp4, video/quicktime, video/webm", "500 MiB"),
            ("audio", "audio/mpeg, audio/wav, audio/mp4, audio/aac", "20 MiB"),
        ],
    )
    body += "<h3>" + ("Требования Seedance к входным файлам" if ru else "Seedance input file requirements") + "</h3>"
    body += (
        "<p>"
        + (
            "Успешная загрузка ещё не означает, что формат подходит модели. Seedance/MiniMax принимают HTTPS URL "
            "без логина и пароля, доступные на всё время выполнения; не передавайте base64 в их JSON. "
            "Тело JSON этих моделей должно быть меньше 1 MiB. Для собственного хранилища используйте доступный извне "
            "HTTPS URL. Таблица ниже — более строгие требования именно Seedance."
            if ru
            else "A successful upload does not establish model compatibility. Seedance/MiniMax require "
            "HTTPS URLs without "
            "embedded credentials, accessible throughout execution; do not embed base64 in their JSON. "
            "Their JSON body must be smaller than 1 MiB. For your own storage, use a publicly reachable HTTPS URL. "
            "The following stricter requirements apply specifically to Seedance."
        )
        + "</p>"
    )
    body += table(
        ("Input", "Seedance 2.5", "Seedance 2.0 / Mini / Fast"),
        [
            (
                "Image",
                "JPEG/PNG; ≤20 MiB; 300–6000 px/side; ratio 0.4–2.5",
                "JPEG/PNG; ≤20 MiB",
            ),
            (
                "Video",
                "MP4/MOV; 2–30 s/file, ≤30 s total; ≤100 MiB/file; 24–60 fps; "
                "300–6000 px/side; 409600–8295044 pixels; ratio 0.4–2.5",
                "MP4/MOV; 2–15 s/file, ≤15 s total; ≤50 MB/file; 200–2160 px/side",
            ),
            (
                "Audio",
                "WAV/MP3; 2–30 s/file, ≤30 s total; ≤15 MB/file",
                "WAV/MP3; 2–15 s/file, ≤15 s total; ≤15 MB/file",
            ),
        ],
    )
    return body


def render_errors(lang):
    def t(ru, en):
        return ru if lang == "ru" else en

    body = '<h2 id="errors">' + t("Ошибки и повторные запросы", "Errors and repeated requests") + "</h2>"
    body += (
        "<p>"
        + t(
            "Проверяйте HTTP status до обработки результата. Ошибки имеют один из двух форматов ниже; detail "
            "также может быть массивом ошибок проверки запроса. Сохраняйте request_id или X-Request-Id для поддержки.",
            "Check HTTP status before processing results. Errors use either format below; detail may also be an array "
            "of request validation errors. Keep request_id or X-Request-Id when contacting support.",
        )
        + "</p>"
    )
    body += code({"detail": "invalid_request_contract"})
    body += (
        "<p>"
        + t(
            "При 422 invalid_request_contract заголовок X-Validation-Error может содержать безопасный код причины: "
            "edit_requires_single_video, invalid_duration, invalid_generate_audio или unsupported_aspect_ratio. "
            "Тело ответа остаётся прежним; неизвестные внутренние ошибки не раскрываются. "
            "Отклонение на request_validation происходит до резервирования денег и отправки провайдеру.",
            "For 422 invalid_request_contract, X-Validation-Error may contain a safe reason code: "
            "edit_requires_single_video, invalid_duration, invalid_generate_audio or unsupported_aspect_ratio. "
            "The response body is unchanged; unknown internal errors are not disclosed. "
            "Rejection at request_validation happens before balance reservation or provider submission.",
        )
        + "</p>"
    )

    body += code(
        {
            "error": {"type": "request_already_submitted", "message": "request_already_submitted"},
            "request_id": "<our-request-id>",
        }
    )
    rows = [
        (
            "401",
            "api_key_required / invalid_api_key",
            t("Проверьте ключ и активность аккаунта.", "Check key and account status."),
        ),
        (
            "402",
            "insufficient_balance",
            t("Недостаточно доступного баланса для запроса.", "Insufficient available balance."),
        ),
        (
            "403",
            "partner_not_active",
            t("Обратитесь за активацией аккаунта.", "Contact support for account activation."),
        ),
        (
            "404",
            "model_not_available / generation_not_found / content_not_available",
            t(
                "Проверьте Model ID, request_id и готовность файла. Чужие запросы недоступны.",
                "Check model ID, request_id and file readiness. Other accounts' requests are not accessible.",
            ),
        ),
        (
            "409",
            "idempotency_conflict",
            t(
                "Этот ключ уже использован с другим телом или методом.",
                "The key was used with a different body or method.",
            ),
        ),
        (
            "409",
            "request_already_submitted",
            t(
                "Синхронный запрос уже принят, результат повторно не возвращается.",
                "The synchronous request was accepted; its result is not replayed.",
            ),
        ),
        (
            "409",
            "capability_mismatch / media_asset_not_ready",
            t("Режим/размер недоступен либо файл ещё не готов.", "Mode/size unavailable, or file not ready."),
        ),
        (
            "415",
            "multipart_not_supported_for_protocol",
            t("Multipart доступен только для images/edits.", "Multipart is supported only for images/edits."),
        ),
        (
            "415",
            "media_upload_requires_json",
            t(
                "Создайте upload ticket через application/json с описанием файла. "
                "Затем отправьте байты отдельным PUT по upload_url.",
                "Create an upload ticket using application/json file metadata. "
                "Then send the bytes with a separate PUT to upload_url.",
            ),
        ),
        (
            "422",
            "invalid_request_contract / invalid_idempotency_key / invalid_previous_response",
            t(
                "Проверьте корректность JSON, параметры, сочетания полей или длину ключа.",
                "Check JSON syntax, parameters, field combinations or key length.",
            ),
        ),
        (
            "422 / 503",
            "upload_rejected / provider_rejected_request",
            t(
                "Модель, файл или запрос не принят. Проверьте требования выбранной модели.",
                "Model, file or request rejected. Check the selected model's requirements.",
            ),
        ),
        ("422", "unknown_model_contract", t("Неизвестный Model ID.", "Unknown model ID.")),
        (
            "429",
            "provider_rate_limited",
            t(
                "Достигнут лимит запросов; учитывайте Retry-After, если присутствует.",
                "Request limit reached; respect Retry-After when present.",
            ),
        ),
        (
            "503",
            "provider_temporarily_unavailable / upload_unavailable",
            t(
                "Сервис временно недоступен. Сохраните ID и проверьте состояние запроса.",
                "Service temporarily unavailable. Preserve the ID and check request state.",
            ),
        ),
        (
            "503",
            "submission_outcome_unknown / provider_response_invalid / unexpected_provider_stream",
            t(
                "Результат отправки не определён. Не создавайте новый платный запрос автоматически; "
                "обратитесь в поддержку.",
                "Submission outcome is uncertain. Do not automatically create another paid request; contact support.",
            ),
        ),
    ]
    body += table(("HTTP", "code", t("Что делать", "Action")), rows)
    body += (
        "<p>"
        + t(
            "Для видео повторите то же тело с тем же Idempotency-Key: вернётся исходный request_id. "
            "При наличии ID достаточно GET статуса. Для текста/изображений повтор с тем же ключом возвращает 409; "
            "сохранённый статус изображения доступен по нашему ID или X-Client-Request-Id. Новый ключ означает новую "
            "операцию и может привести к повторной оплате. "
            "При timeout, обрыве сети или 5xx не меняйте ключ для автоматической повторной отправки. "
            "У клиентов с автоматическими повторами POST отключите их; при однозначно отклонённом запросе "
            "исправленный новый запрос отправляют с новым ключом. Фиксированная общая квота RPS здесь не "
            "гарантируется.",
            "For video, resubmit the same body with the same Idempotency-Key to get the original request_id. "
            "If you have the ID, simply GET its status. For text/images, the same key returns 409; retrieve saved "
            "image status by our ID or X-Client-Request-Id. "
            "A new key means a new operation and may incur another charge. After a timeout, network "
            "interruption or 5xx, "
            "do not change the key for automatic resubmission. Disable automatic paid POST retries in clients. "
            "After a definitive rejection, submit a corrected new request with a new key. A fixed universal RPS quota "
            "is not guaranteed here.",
        )
        + "</p>"
    )
    return body
