"""Client-facing text protocol reference and contract-testable request examples."""

import json
import shlex
from html import escape
from typing import Any

PUBLIC_TEXT_MODELS_REVIEWED = "2026-09-28"

# Deliberately whitelisted public capabilities; absent fields mean unconfirmed.
PUBLIC_TEXT_MODELS: dict[str, dict[str, Any]] = {
    "gpt-6-astra": {
        "context_tokens": 1050000,
        "max_input_tokens": 922000,
        "max_output_tokens": 128000,
        "images": True,
        "efforts": ["low", "medium", "high", "xhigh", "max"],
    },
    "gpt-5.6-luna": {
        "context_tokens": 1050000,
        "max_input_tokens": 922000,
        "max_output_tokens": 128000,
        "images": True,
        "efforts": ["low", "medium", "high", "xhigh", "max"],
        "default_effort": "medium",
    },
    "gpt-5.6-terra": {
        "context_tokens": 1050000,
        "max_input_tokens": 922000,
        "max_output_tokens": 128000,
        "images": True,
        "efforts": ["low", "medium", "high", "xhigh", "max"],
        "default_effort": "medium",
    },
    "gpt-5.6-sol": {
        "context_tokens": 1050000,
        "max_input_tokens": 922000,
        "max_output_tokens": 128000,
        "images": True,
        "efforts": ["low", "medium", "high", "xhigh", "max"],
        "default_effort": "medium",
    },
    "claude-opus-5": {
        "context_tokens": 1000000,
        "max_output_tokens": 128000,
        "images": True,
        "efforts": ["low", "medium", "high", "xhigh", "max"],
        "default_effort": "high",
        "thinking": "switchable",
    },
    "claude-fable-5-1": {
        "context_tokens": 1000000,
        "max_output_tokens": 128000,
        "images": True,
        "efforts": ["low", "medium", "high", "xhigh", "max"],
        "default_effort": "high",
        "thinking": "always",
    },
    "claude-fable-5": {
        "context_tokens": 1000000,
        "max_output_tokens": 128000,
        "images": True,
        "efforts": ["low", "medium", "high", "xhigh", "max"],
        "default_effort": "high",
        "thinking": "always",
    },
    "grok-4.6": {
        "context_tokens": 500000,
        "images": True,
        "efforts": ["low", "medium", "high", "xhigh"],
        "effort_aliases": {"max": "high"},
        "default_effort": "high",
        "thinking": "always",
    },
    "claude-opus-4-8": {
        "context_tokens": 1000000,
        "max_output_tokens": 128000,
        "images": True,
        "efforts": ["low", "medium", "high", "xhigh", "max"],
        "default_effort": "high",
        "thinking": "opt_in",
    },
    "claude-sonnet-5": {
        "context_tokens": 1000000,
        "max_output_tokens": 128000,
        "images": True,
        "efforts": ["low", "medium", "high", "xhigh", "max"],
        "default_effort": "high",
        "thinking": "switchable",
    },
    "deepseek-v4-flash-vision-exp": {},
    "deepseek-v4-pro-0813": {},
    "glm-5.1": {"context_tokens": 200000, "max_output_tokens": 128000},
    "glm-5.2": {
        "context_tokens": 1000000,
        "max_output_tokens": 128000,
        "efforts": ["high", "max"],
        "effort_aliases": {"low": "high", "medium": "high", "xhigh": "max"},
        "default_effort": "max",
    },
    "glm-5.3": {
        "context_tokens": 1000000,
        "max_output_tokens": 128000,
        "efforts": ["low", "high", "max"],
        "effort_aliases": {"medium": "high", "xhigh": "max"},
        "default_effort": "max",
        "thinking": "always",
    },
    "glm-5.3-flash": {
        "context_tokens": 1000000,
        "max_output_tokens": 128000,
        "images": True,
        "efforts": ["high", "max"],
        "effort_aliases": {"low": "high", "medium": "high", "xhigh": "max"},
        "default_effort": "max",
        "thinking": "always",
    },
    "gpt-5.3-codex-spark": {},
    "gpt-5.4": {
        "context_tokens": 1050000,
        "max_output_tokens": 128000,
        "images": True,
        "efforts": ["low", "medium", "high", "xhigh"],
        "default_effort": "none",
    },
    "gpt-5.4-mini": {
        "context_tokens": 400000,
        "max_input_tokens": 272000,
        "max_output_tokens": 128000,
        "images": True,
        "efforts": ["low", "medium", "high", "xhigh"],
        "default_effort": "none",
    },
    "gpt-5.5": {
        "context_tokens": 1050000,
        "max_output_tokens": 128000,
        "images": True,
        "efforts": ["low", "medium", "high", "xhigh"],
        "default_effort": "medium",
    },
    "grok-4.5": {
        "context_tokens": 500000,
        "images": True,
        "efforts": ["low", "medium", "high"],
        "effort_aliases": {"max": "high", "xhigh": "high"},
        "default_effort": "high",
        "thinking": "always",
    },
    "kimi-k2.6": {"context_tokens": 262144},
    "kimi-k2.7-code": {"context_tokens": 262144, "thinking": "always"},
    "kimi-k3": {
        "context_tokens": 1048576,
        "max_output_tokens": 1048576,
        "default_output_tokens": 131072,
        "images": True,
        "efforts": ["low", "high", "max"],
        "default_effort": "max",
        "thinking": "always",
    },
}


EXAMPLES: list[dict[str, Any]] = [
    {
        "protocol": "responses",
        "body": {
            "model": "gpt-5.6-sol",
            "input": "Explain a database transaction in two sentences.",
            "max_output_tokens": 512,
        },
    },
    {
        "protocol": "chat/completions",
        "body": {
            "model": "glm-5.3",
            "messages": [{"role": "user", "content": "Explain a database transaction in two sentences."}],
            "max_completion_tokens": 2048,
            "reasoning_effort": "low",
        },
    },
    {
        "protocol": "messages",
        "body": {
            "model": "claude-sonnet-5",
            "max_tokens": 512,
            "messages": [{"role": "user", "content": "Explain a database transaction in two sentences."}],
        },
    },
    {
        "protocol": "responses",
        "body": {
            "model": "gpt-5.6-sol",
            "input": "Check order A123 using the available tool.",
            "max_output_tokens": 512,
            "tools": [
                {
                    "type": "function",
                    "name": "get_order",
                    "description": "Read an order status.",
                    "parameters": {
                        "type": "object",
                        "properties": {"order_id": {"type": "string"}},
                        "required": ["order_id"],
                        "additionalProperties": False,
                    },
                    "strict": True,
                }
            ],
            "tool_choice": "auto",
        },
    },
    {
        "protocol": "chat/completions",
        "body": {
            "model": "gpt-5.6-sol",
            "messages": [{"role": "user", "content": "Check order A123 using the available tool."}],
            "max_completion_tokens": 512,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_order",
                        "description": "Read an order status.",
                        "parameters": {
                            "type": "object",
                            "properties": {"order_id": {"type": "string"}},
                            "required": ["order_id"],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
            "tool_choice": "auto",
        },
    },
    {
        "protocol": "messages",
        "body": {
            "model": "claude-sonnet-5",
            "max_tokens": 512,
            "messages": [{"role": "user", "content": "Check order A123 using the available tool."}],
            "tools": [
                {
                    "name": "get_order",
                    "description": "Read an order status.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"order_id": {"type": "string"}},
                        "required": ["order_id"],
                    },
                }
            ],
            "tool_choice": {"type": "auto"},
        },
    },
    {
        "protocol": "responses",
        "body": {
            "model": "gpt-5.6-sol",
            "input": "Return the word ready in a status field.",
            "max_output_tokens": 512,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "status_result",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {"status": {"type": "string"}},
                        "required": ["status"],
                        "additionalProperties": False,
                    },
                }
            },
        },
    },
    {
        "protocol": "chat/completions",
        "body": {
            "model": "gpt-5.6-sol",
            "messages": [{"role": "user", "content": "Return the word ready in a status field."}],
            "max_completion_tokens": 512,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "status_result",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {"status": {"type": "string"}},
                        "required": ["status"],
                        "additionalProperties": False,
                    },
                },
            },
        },
    },
    {
        "protocol": "responses",
        "body": {
            "model": "gpt-5.6-sol",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Describe the image."},
                        {"type": "input_image", "image_url": "https://example.com/reference.jpg"},
                    ],
                }
            ],
            "max_output_tokens": 512,
        },
    },
    {
        "protocol": "chat/completions",
        "body": {
            "model": "gpt-5.6-sol",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe the image."},
                        {"type": "image_url", "image_url": {"url": "https://example.com/reference.jpg"}},
                    ],
                }
            ],
            "max_completion_tokens": 512,
        },
    },
    {
        "protocol": "messages",
        "body": {
            "model": "claude-sonnet-5",
            "max_tokens": 512,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe the image."},
                        {"type": "image", "source": {"type": "url", "url": "https://example.com/reference.jpg"}},
                    ],
                }
            ],
        },
    },
    {
        "protocol": "chat/completions",
        "body": {
            "model": "kimi-k3",
            "messages": [{"role": "user", "content": "List three checks before saving a payment."}],
            "max_completion_tokens": 2048,
            "reasoning_effort": "low",
            "stream": True,
        },
    },
]


def _code(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
    return "<pre><code>" + escape(text) + "</code></pre>"


def _table(ru: bool, rows: list[tuple[str, str, str, str]]) -> str:
    headers = (
        ("Поле", "Тип", "Обязательность / значение", "Назначение")
        if ru
        else (
            "Field",
            "Type",
            "Required / value",
            "Meaning",
        )
    )
    return (
        '<div class="table-scroll"><table><thead><tr>'
        + "".join(f"<th>{escape(h)}</th>" for h in headers)
        + "</tr></thead><tbody>"
        + "".join("<tr>" + "".join(f"<td>{escape(c)}</td>" for c in row) + "</tr>" for row in rows)
        + "</tbody></table></div>"
    )


def _render_model_limits(ru: bool) -> str:
    unknown = "Не объявлено" if ru else "Not declared"
    headers = (
        ("Model ID", "Контекст, токены", "Макс. вход", "Макс. вывод", "Вывод по умолчанию", "Vision")
        if ru
        else ("Model ID", "Context tokens", "Max input", "Max output", "Default output", "Vision")
    )
    rows = []
    for model, data in PUBLIC_TEXT_MODELS.items():
        vision = (
            ("Да" if ru else "Yes")
            if data.get("images") is True
            else (("Нет" if ru else "No") if data.get("images") is False else unknown)
        )
        values = [model]
        for key in ("context_tokens", "max_input_tokens", "max_output_tokens", "default_output_tokens"):
            values.append(f"{data[key]:,}".replace(",", " ") if key in data else unknown)
        values.append(vision)
        rows.append("<tr>" + "".join(f"<td>{escape(value)}</td>" for value in values) + "</tr>")
    result = (
        '<h3 id="text-model-limits">'
        + ("Лимиты и возможности текстовых моделей" if ru else "Text model limits and capabilities")
        + "</h3><p>"
        + escape(
            "Проверено 28.09.2026. Здесь перечислены объявленные технические параметры, а не обещание "
            "доступности всех ID: работающая модель должна присутствовать в GET /v1/models. Контекст включает "
            "вход и вывод вместе; максимумы входа и вывода не всегда достижимы одновременно. Системные "
            "инструкции, история, инструменты и рассуждения также используют токены. Не объявлено означает, "
            "что подтверждённого значения нет; это не ноль, не отсутствие ограничения и не поддержка функции. "
            "API проверяет общий формат, а конкретная модель может отклонить превышение своего лимита."
            if ru
            else "Reviewed 2026-09-28. These are declared technical capabilities, not a promise that every ID is "
            "available: an enabled model must appear in GET /v1/models. Context includes input and output "
            "together; maximum input and output may not be achievable simultaneously. System instructions, "
            "history, tools and reasoning also consume tokens. Not declared means there is no confirmed "
            "value; it does not mean zero, unlimited capacity or support for a feature. The API validates "
            "the common format; a model may reject requests that exceed its own limit."
        )
        + "</p>"
    )
    result += (
        '<div class="table-scroll"><table><thead><tr>'
        + "".join(f"<th>{escape(header)}</th>" for header in headers)
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )
    effort_headers = (
        ("Model ID", "Уровни effort", "Дополнительные значения → фактические", "Effort по умолчанию", "Thinking")
        if ru
        else ("Model ID", "Effort levels", "Additional values → effective values", "Default effort", "Thinking")
    )
    thinking_labels = {
        "always": "Всегда включено" if ru else "Always enabled",
        "switchable": "Можно переключать" if ru else "Switchable",
        "opt_in": "Включается явно" if ru else "Opt-in",
    }
    result += (
        "<p>"
        + escape(
            "Следующая таблица отдельно показывает перечисленные уровни, дополнительные значения "
            "и объявленную настройку по умолчанию. Отсутствие default не означает medium. Для GPT с default=none "
            "не подменяйте отсутствие параметра произвольно выбранным уровнем. Способ передачи effort зависит "
            "от протокола и описан ниже. Отдельные правила GLM/Kimi приведены в разделе Reasoning / thinking."
            if ru
            else "The next table separates listed levels, additional accepted values and declared defaults. "
            "An absent default does not imply medium. For GPT models with default=none, do not replace an "
            "omitted field with an arbitrary effort level. The effort field depends on the protocol, as "
            "described below. See Reasoning / thinking for additional GLM/Kimi rules."
        )
        + "</p>"
    )
    rows = []
    for model, data in PUBLIC_TEXT_MODELS.items():
        values = [
            model,
            " / ".join(data["efforts"]) if "efforts" in data else unknown,
            "; ".join(f"{value} → {effective}" for value, effective in data.get("effort_aliases", {}).items())
            or unknown,
            data.get("default_effort", unknown),
            thinking_labels.get(data.get("thinking"), unknown),
        ]
        rows.append("<tr>" + "".join(f"<td>{escape(value)}</td>" for value in values) + "</tr>")
    result += (
        '<div class="table-scroll"><table><thead><tr>'
        + "".join(f"<th>{escape(header)}</th>" for header in effort_headers)
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )
    result += (
        "<p>"
        + escape(
            "Claude Opus 5: отключение thinking допускается только при effort high или ниже. Claude Fable 5.1: "
            "не используйте принудительный tool_choice any/tool. Claude Sonnet 5: оставьте temperature, top_p "
            "и top_k неуказанными — произвольное изменение этих настроек не поддерживается. Для "
            "deepseek-v4-flash-vision-exp, deepseek-v4-pro-0813 и gpt-5.3-codex-spark текущие пределы не "
            "подтверждены; значения и настройки других ID на них автоматически не распространяются."
            if ru
            else "Claude Opus 5: thinking can be disabled only at effort high or lower. Claude Fable 5.1: do not "
            "force a tool with tool_choice any/tool. Claude Sonnet 5: omit temperature, top_p and top_k; "
            "arbitrary overrides of these settings are unsupported. Current limits are unconfirmed for "
            "deepseek-v4-flash-vision-exp, deepseek-v4-pro-0813 and gpt-5.3-codex-spark; settings from other "
            "IDs do not automatically apply to them."
        )
        + "</p>"
    )
    return result


def _curl(example: dict[str, Any]) -> str:
    command = [
        'curl --fail-with-body -sS -N "$API_BASE/v1/' + example["protocol"] + '"',
        '  -H "Authorization: Bearer $API_KEY"',
        '  -H "Idempotency-Key: $REQUEST_KEY"',
        '  -H "Content-Type: application/json"',
    ]
    if example["protocol"] == "messages":
        command.append('  -H "anthropic-version: 2023-06-01"')
    command.append("  --data " + shlex.quote(json.dumps(example["body"], indent=2, ensure_ascii=False)))
    return _code(" \\\n".join(command))


def render_text_reference(lang: str) -> str:
    """Render only public inference parameters; examples never imply availability."""
    ru = lang == "ru"

    def choose(russian: str, english: str) -> str:
        return russian if ru else english

    def paragraph(russian: str, english: str) -> str:
        return "<p>" + escape(choose(russian, english)) + "</p>"

    optional = choose("Нет; зависит от модели", "No; model-specific")
    required = choose("Да", "Yes")
    result = '<section aria-labelledby="text"><h2 id="text">' + choose("Текстовые модели", "Text models") + "</h2>"
    result += paragraph(
        "Выберите включённую текстовую модель из GET /v1/models. ID в примерах показывают формат запроса; "
        "их наличие в справочнике не означает, что модель сейчас доступна. GPT-модели вызывайте через Responses "
        "или Chat Completions. Messages подходит для Claude и совместимых моделей. Возможности vision, tools, "
        "JSON Schema и reasoning зависят от выбранной модели и не становятся доступными при смене протокола.",
        "Select an enabled text model from GET /v1/models. Example IDs illustrate request syntax and do not "
        "indicate current availability. Use Responses or Chat Completions for GPT models. Messages is for "
        "Claude and compatible models. Vision, tools, JSON Schema and reasoning capabilities depend on the "
        "selected model; changing the protocol does not add capabilities.",
    )
    result += '<h3 id="text-luna">GPT-5.6 Luna</h3>'
    result += paragraph(
        "gpt-5.6-luna принимает текст и изображения, возвращает текст. Используйте POST /v1/responses "
        "с input либо POST /v1/chat/completions с messages; /v1/messages для этой модели не поддерживается. "
        "Изображения: input_image в Responses, image_url в Chat; подходят публичные HTTPS URL и data URL.",
        "gpt-5.6-luna accepts text and images and returns text. Use POST /v1/responses with input or "
        "POST /v1/chat/completions with messages; /v1/messages is not supported for this model. "
        "Images use input_image in Responses or image_url in Chat, with public HTTPS URLs or data URLs.",
    )
    result += paragraph(
        "reasoning.effort в Responses или reasoning_effort в Chat: low, medium, high, xhigh, max; "
        "по умолчанию medium. Контекст 1 050 000 токенов, вход до 922 000, вывод до 128 000. "
        "Reasoning-токены входят в лимит вывода и оплачиваются как выходные.",
        "Use reasoning.effort in Responses or reasoning_effort in Chat: low, medium, high, xhigh, max; "
        "the default is medium. Context is 1,050,000 tokens, input up to 922,000, output up to 128,000. "
        "Reasoning tokens count toward the output cap and are billed as output tokens.",
    )
    result += paragraph(
        "service_tier — необязательная строка. Опустите её для стандартного режима. Значения priority "
        "или fast включают Fast: все опубликованные рублёвые тарифы этой модели умножаются на 2, "
        "включая вход, выход, чтение и запись кэша. Это правило относится к Luna; наличие Fast у другой "
        "модели проверяйте отдельно.",
        "service_tier is an optional string. Omit it for the standard tier. priority or fast selects "
        "Fast: every published RUB rate for this model is multiplied by 2, including input, output, "
        "cache reads and cache writes. This rule is for Luna; check other models separately for Fast support.",
    )
    result += '<h3 id="text-glm-flash">GLM-5.3 Flash</h3>'
    result += paragraph(
        "Для glm-5.3-flash используйте POST /v1/chat/completions. Модель принимает текст и изображения "
        "image_url с публичными HTTPS URL или data URL и возвращает текст. Контекст 1 000 000 токенов, "
        "вывод до 128 000. Thinking всегда включён; reasoning_effort: high или max, по умолчанию max. "
        "low и medium выполняются как high, xhigh — как max. service_tier не поддерживается: опустите его.",
        "For glm-5.3-flash, use POST /v1/chat/completions. The model accepts text and image_url inputs "
        "with public HTTPS URLs or data URLs and returns text. Context is 1,000,000 tokens, output up to "
        "128,000. Thinking is always enabled; reasoning_effort accepts high or max, default max. "
        "low and medium run as high; xhigh runs as max. service_tier is not supported: omit it.",
    )
    result += paragraph(
        "Responses и Messages доступны через преобразование в Chat Completions с ограничениями. "
        "В Responses передавайте текст или input_image; input_file не переносится. В Messages "
        "поддерживаются текст, tool-блоки и изображения только в base64; изображения по URL не переносятся. "
        "Web search и другие серверные инструменты при обоих преобразованиях не поддерживаются. "
        "reasoning.effort в Responses использует те же уровни. В Messages явно задавайте "
        "output_config.effort: без него передаётся medium, который модель выполняет как high.",
        "Responses and Messages are available through conversion to Chat Completions with limitations. "
        "Responses accepts text or input_image; input_file is not carried over. Messages supports text, "
        "tool blocks and base64 images only; URL images are not carried over. Web search and other "
        "server-side tools are unsupported in both conversions. reasoning.effort in Responses uses the "
        "same levels. Set output_config.effort explicitly in Messages: when omitted, medium is sent, "
        "which this model runs as high.",
    )
    result += paragraph(
        "Все три метода принимают JSON и требуют Authorization: Bearer, Content-Type: application/json "
        "и уникальный Idempotency-Key для каждой новой операции. В примерах API_BASE — указанный выше адрес "
        "без /v1 в конце, API_KEY — ваш ключ. Создайте REQUEST_KEY один раз, сохраните вместе с телом запроса; "
        "повтор той же операции использует тот же ключ. Для нового запроса создайте новый ключ.",
        "All three methods accept JSON and require Authorization: Bearer, Content-Type: application/json "
        "and a unique Idempotency-Key for every new operation. In these examples API_BASE is the base URL "
        "shown above, without a trailing /v1, and API_KEY is your key. Create REQUEST_KEY once and save it "
        "with the request body. Reuse it for the same operation; create another key for a new request.",
    )
    result += _code("REQUEST_KEY=\"$(python3 -c 'import uuid; print(uuid.uuid4())')\"")
    result += _table(
        ru,
        [
            ("model", "string", required, choose("Точный ID текстовой модели.", "Exact text model ID.")),
            (
                "stream",
                "boolean",
                choose("Нет; обычно false", "No; normally false"),
                choose("true — SSE; false — один JSON-ответ.", "true selects SSE; false requests one JSON response."),
            ),
            (
                "max_output_tokens / max_completion_tokens / max_tokens",
                "integer",
                "1–10,000,000",
                choose(
                    "Используйте поле своего протокола, описанное ниже. Это граница проверки API, а не лимит модели. "
                    "Фактический максимум вывода и общий контекст могут быть меньше. Указывайте явный предел; "
                    "reasoning-токены могут входить в него.",
                    "Use the field for your protocol below. This is the API validation boundary, not a model "
                    "limit. Actual output and context limits may be lower. Set an explicit cap; reasoning "
                    "tokens may consume it.",
                ),
            ),
            (
                "temperature / top_p",
                "number",
                optional,
                choose(
                    "Управление вариативностью. Диапазоны и совместимость с reasoning зависят от модели; "
                    "при первом запросе опустите оба поля.",
                    "Sampling controls. Ranges and reasoning compatibility depend on the model; omit both "
                    "for the first request.",
                ),
            ),
        ],
    )
    result += _render_model_limits(ru)

    result += '<h3 id="text-responses">POST /v1/responses</h3>'
    result += _table(
        ru,
        [
            (
                "input",
                "string | array",
                required,
                choose(
                    "Текст или сообщения {role, content}. content — строка либо список input_text/input_image "
                    "блоков. role обычно user/assistant/system/developer.",
                    "Text or {role, content} messages. content is a string or a list of input_text/input_image "
                    "blocks. Roles are typically user/assistant/system/developer.",
                ),
            ),
            (
                "instructions",
                "string",
                optional,
                choose("Инструкции для текущего запроса.", "Instructions for this request."),
            ),
            ("max_output_tokens", "integer", optional, choose("Верхняя граница вывода.", "Output token cap.")),
            (
                "previous_response_id",
                "string",
                optional,
                choose(
                    "id завершённого ответа этого API, полученного вашим аккаунтом. Не все модели поддерживают "
                    "продолжение. При invalid_previous_response передайте историю явно новым запросом.",
                    "id of a completed response from this API for your account. Continuation is model-specific. "
                    "For invalid_previous_response, submit a new request with explicit conversation history.",
                ),
            ),
            (
                "tools / tool_choice",
                "array / string | object",
                optional,
                choose(
                    "Функции с type=function, name, parameters; выбор auto/none/required или конкретной функции.",
                    "Functions with type=function, name and parameters; select auto/none/required or a function.",
                ),
            ),
            (
                "parallel_tool_calls",
                "boolean",
                optional,
                choose(
                    "Разрешение нескольких вызовов функций в одном ответе.",
                    "Allow multiple function calls in one response.",
                ),
            ),
            (
                "reasoning",
                "object",
                optional,
                choose(
                    'Например {"effort":"low"}. Набор уровней зависит от модели.',
                    'For example {"effort":"low"}. Accepted levels depend on the model.',
                ),
            ),
            (
                "text.format",
                "object",
                optional,
                choose(
                    "Формат текста; json_schema задаётся через name, schema и strict. См. пример ниже.",
                    "Text format; json_schema uses name, schema and strict. See the example below.",
                ),
            ),
            (
                "store / metadata",
                "boolean / object",
                optional,
                choose(
                    "Дополнительные настройки поддерживающих их моделей; не являются гарантией хранения "
                    "или отдельного метода чтения ответа.",
                    "Additional options for models that support them; these do not promise retention or "
                    "a separate response retrieval method.",
                ),
            ),
        ],
    )
    result += _curl(EXAMPLES[0])
    result += paragraph(
        "HTTP 200: читайте output[] и собирайте блоки content[] с type=output_text. Массив может также "
        "содержать reasoning и function_call. Не привязывайте извлечение текста к одному индексу. "
        "status=incomplete означает частичный результат; проверяйте incomplete_details.",
        "HTTP 200: traverse output[] and collect content[] blocks with type=output_text. The array can "
        "also contain reasoning and function_call items. Do not assume a fixed text index. "
        "status=incomplete means a partial result; inspect incomplete_details.",
    )
    result += _code(
        {
            "id": "8488e6a4-d38a-4f95-a831-752aa12bf5d7",
            "object": "response",
            "status": "completed",
            "output": [
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Ready."}]}
            ],
            "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
        }
    )

    result += '<h3 id="text-chat">POST /v1/chat/completions</h3>'
    result += _table(
        ru,
        [
            (
                "messages",
                "array",
                required,
                choose(
                    "История {role, content}: system/developer/user/assistant/tool в порядке диалога. "
                    "content — строка или блоки text/image_url; доступные роли зависят от модели.",
                    "Ordered conversation {role, content}: system/developer/user/assistant/tool. "
                    "content is text or text/image_url blocks; supported roles depend on the model.",
                ),
            ),
            (
                "max_completion_tokens",
                "integer",
                optional,
                choose("Предпочтительное поле предела вывода.", "Preferred output cap field."),
            ),
            (
                "max_tokens",
                "integer",
                optional,
                choose(
                    "Совместимое поле для моделей, использующих прежний формат. Не задавайте одновременно "
                    "с max_completion_tokens.",
                    "Compatibility cap for models using the earlier format. Do not combine with max_completion_tokens.",
                ),
            ),
            (
                "tools / tool_choice",
                "array / string | object",
                optional,
                choose(
                    "tools[].type=function, описание внутри tools[].function; выбор auto/none/required "
                    "либо конкретной функции.",
                    "tools[].type=function, with definitions in tools[].function; select auto/none/required "
                    "or a named function.",
                ),
            ),
            (
                "parallel_tool_calls",
                "boolean",
                optional,
                choose("Разрешение нескольких функций в ответе.", "Allow multiple functions per response."),
            ),
            (
                "response_format",
                "object",
                optional,
                choose(
                    "type=json_object для JSON или type=json_schema с вложенным json_schema для схемы. "
                    "Просите JSON также в тексте запроса.",
                    "type=json_object for JSON, or type=json_schema with nested json_schema for a schema. "
                    "Also request JSON in the prompt.",
                ),
            ),
            (
                "reasoning_effort / thinking",
                "string / object",
                optional,
                choose(
                    "См. таблицу моделей ниже; это разные настройки, а не взаимозаменяемые имена.",
                    "See the model table below; these are distinct controls, not aliases.",
                ),
            ),
            (
                "stream_options",
                "object",
                optional,
                choose(
                    "При stream=true API запрашивает include_usage=true; обработайте завершающий блок "
                    "usage, даже если choices пуст.",
                    "With stream=true the API requests include_usage=true; process the final usage chunk "
                    "even when choices is empty.",
                ),
            ),
            (
                "stop / seed / presence_penalty / frequency_penalty",
                "string | array / integer / number / number",
                optional,
                choose(
                    "Остановка, подсказка генератору и штрафы повторения только у поддерживающих их моделей. "
                    "seed не гарантирует точное воспроизведение.",
                    "Stop sequences, sampling seed and repetition controls only for supporting models. "
                    "seed does not guarantee exact reproduction.",
                ),
            ),
        ],
    )
    result += _curl(EXAMPLES[1])
    result += paragraph(
        "HTTP 200: текст находится в choices[].message.content, вызовы функций — в message.tool_calls, "
        "причина завершения — finish_reason. length означает достижение лимита и возможный обрыв текста.",
        "HTTP 200: text is in choices[].message.content, function calls in message.tool_calls and "
        "the termination reason in finish_reason. length indicates that a cap was reached and text may be truncated.",
    )
    result += _code(
        {
            "id": "8488e6a4-d38a-4f95-a831-752aa12bf5d7",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Ready."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
        }
    )

    result += '<h3 id="text-messages">POST /v1/messages</h3>'
    result += paragraph(
        "Передайте anthropic-version: 2023-06-01. Вместо Bearer допускается x-api-key с вашим ключом, "
        "если Authorization отсутствует. Дополнительный anthropic-beta используйте только для "
        "подтверждённой возможности выбранной модели.",
        "Send anthropic-version: 2023-06-01. You may use x-api-key with your key when Authorization is "
        "absent. Use the optional anthropic-beta header only for a confirmed feature of the chosen model.",
    )
    result += _table(
        ru,
        [
            (
                "messages",
                "array",
                required,
                choose(
                    "Сообщения user/assistant; content — строка или блоки text/image/tool_use/tool_result.",
                    "user/assistant messages; content is text or text/image/tool_use/tool_result blocks.",
                ),
            ),
            (
                "max_tokens",
                "integer",
                required,
                choose(
                    "Предел вывода. Обязателен даже при stream=true.", "Output cap, required even with stream=true."
                ),
            ),
            (
                "system",
                "string | array",
                optional,
                choose(
                    "Отдельная системная инструкция или текстовые блоки. Роль system в messages не используется.",
                    "Separate system instructions or text blocks. Do not use a system role inside messages.",
                ),
            ),
            (
                "tools / tool_choice",
                "array / object",
                optional,
                choose(
                    "Определения name, description, input_schema; выбор {type:auto}, {type:any}, "
                    "{type:tool,name:...} или {type:none}, если поддерживается моделью.",
                    "Definitions with name, description and input_schema; choices {type:auto}, {type:any}, "
                    "{type:tool,name:...} or {type:none}, when supported by the model.",
                ),
            ),
            (
                "thinking",
                "object",
                optional,
                choose(
                    "Включение/режим рассуждений. Форма type и budget_tokens зависит от поколения модели; "
                    "budget_tokens не заменяет обязательный max_tokens.",
                    "Reasoning mode. type and budget_tokens depend on the model generation; budget_tokens "
                    "does not replace required max_tokens.",
                ),
            ),
            (
                "output_config",
                "object",
                optional,
                choose(
                    "Уровень effort и формат вывода у поддерживающих моделей. Не переносите сюда "
                    "объект response_format из Chat Completions.",
                    "Effort and output format for supporting models. Do not copy a Chat Completions "
                    "response_format object into this field.",
                ),
            ),
            (
                "stop_sequences / top_k / metadata",
                "array / integer / object",
                optional,
                choose(
                    "Последовательности остановки, ограничение выборки, метаданные; поддержка зависит от модели.",
                    "Stop sequences, sampling cutoff and metadata; availability depends on the model.",
                ),
            ),
        ],
    )
    result += _curl(EXAMPLES[2])
    result += paragraph(
        "HTTP 200: content[] содержит text, tool_use и другие блоки модели. Читайте все text-блоки; "
        "stop_reason=max_tokens указывает на ограниченный вывод, stop_reason=tool_use — на запрос инструмента. "
        "Здесь счётчики usage называются input_tokens и output_tokens.",
        "HTTP 200: content[] contains text, tool_use and other model blocks. Read every text block; "
        "stop_reason=max_tokens indicates capped output, while tool_use requests a tool. "
        "The usage counters are input_tokens and output_tokens.",
    )
    result += _code(
        {
            "id": "8488e6a4-d38a-4f95-a831-752aa12bf5d7",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "Ready."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 12, "output_tokens": 3},
        }
    )

    result += '<h3 id="text-tools">' + choose("Инструменты и JSON Schema", "Tools and JSON Schema") + "</h3>"
    result += paragraph(
        "Ниже тела запросов к соответствующим методам; заголовки те же. Приложение выполняет функцию само "
        "после проверки её аргументов и разрешений. Результат отправляйте следующим запросом с новым "
        "Idempotency-Key. В Responses это input-элемент type=function_call_output с call_id и строкой output; "
        "в Chat — сообщение role=tool с tool_call_id и строкой content после сообщения assistant с tool_calls; "
        "в Messages — user-блок type=tool_result с tool_use_id и content после assistant-блока tool_use. "
        "Сохраняйте исходные идентификаторы вызовов и историю.",
        "The bodies below use the corresponding endpoints and the same headers. Your application executes "
        "functions after checking arguments and permissions. Send the result in a subsequent request with "
        "a new Idempotency-Key. Responses uses an input item with type=function_call_output, call_id and "
        "string output. Chat uses a role=tool message with tool_call_id and string content after the assistant "
        "tool_calls message. Messages uses a user tool_result block with tool_use_id and content after the "
        "assistant tool_use block. Preserve call identifiers and conversation history.",
    )
    for example in EXAMPLES[3:6]:
        result += "<h4>" + escape(example["protocol"]) + " — tools</h4>" + _code(example["body"])
    result += paragraph(
        "Для строгой схемы все обязательные свойства перечислены в required; additionalProperties=false "
        "запрещает лишние поля. Валидируйте JSON в приложении и обрабатывайте отказ модели или достижение "
        "лимита токенов. Поддержка structured output не следует только из принятия поля API.",
        "For a strict schema, list required properties in required; additionalProperties=false excludes "
        "extra keys. Validate returned JSON in your application and handle model refusals and token caps. "
        "Acceptance of a field alone does not establish structured-output support.",
    )
    for example in EXAMPLES[6:8]:
        result += "<h4>" + escape(example["protocol"]) + " — JSON Schema</h4>" + _code(example["body"])

    result += (
        '<h3 id="text-vision">' + choose("Изображение в текстовом запросе", "Image input for text models") + "</h3>"
    )
    result += paragraph(
        "Используйте только vision-модель. Замените https://example.com/reference.jpg на прямую доступную "
        "HTTPS-ссылку на изображение. Base64 также зависит от модели: в Responses input_image.image_url "
        "и Chat image_url.url передают data:image/jpeg;base64,...; в Messages используют source с "
        "type=base64, media_type=image/jpeg и data с чистым base64. Для Kimi K3 используйте "
        "base64 data URL. Максимальный размер/число изображений и "
        "поддержка файлов и аудио не являются общими для всех текстовых моделей.",
        "Choose a vision-capable model. Replace https://example.com/reference.jpg with a directly "
        "accessible HTTPS image URL. Base64 support is model-specific: Responses input_image.image_url "
        "and Chat image_url.url use data:image/jpeg;base64,...; Messages uses a source with type=base64, "
        "media_type=image/jpeg and raw base64 data. For Kimi K3, use base64 data URLs. "
        "Image size/count limits and file or audio input support are not shared across all text models.",
    )
    for example in EXAMPLES[8:11]:
        result += "<h4>" + escape(example["protocol"]) + " — vision</h4>" + _code(example["body"])

    result += '<h3 id="text-reasoning">Reasoning / thinking</h3>'
    result += paragraph(
        "Для GLM и Kimi управляйте reasoning_effort через Chat Completions. Таблица описывает настройку "
        "при доступности соответствующей модели; уровни не универсальны. GPT использует reasoning.effort "
        "в Responses или reasoning_effort в Chat. У Claude thinking и output_config зависят от поколения; "
        "без подтверждённой конфигурации опустите их. HTTP 200 не подтверждает, что произвольная "
        "неподдерживаемая настройка повлияла на результат.",
        "For GLM and Kimi, use reasoning_effort through Chat Completions. The table applies when the "
        "corresponding model is available; levels are not universal. GPT uses reasoning.effort in "
        "Responses or reasoning_effort in Chat. Claude thinking and output_config depend on generation; "
        "omit them unless the configuration is confirmed. HTTP 200 does not prove that an arbitrary "
        "unsupported setting affected the result.",
    )
    result += _table(
        ru,
        [
            (
                "glm-5.1",
                "reasoning_effort",
                "none",
                choose("По умолчанию включено; none отключает.", "Enabled by default; none disables it."),
            ),
            (
                "glm-5.2",
                "reasoning_effort",
                "none / low / medium / high / xhigh / max",
                choose(
                    "По умолчанию max; low/medium/high → high, xhigh/max → max; none отключает.",
                    "Default max; low/medium/high → high, xhigh/max → max; none disables it.",
                ),
            ),
            (
                "glm-5.3",
                "reasoning_effort",
                "low / high / max",
                choose("По умолчанию max; отключение не поддерживается.", "Default max; cannot be disabled."),
            ),
            (
                "glm-5.3-flash",
                "reasoning_effort",
                "high / max",
                choose(
                    "По умолчанию max; low/medium → high, xhigh → max; всегда включено.",
                    "Default max; low/medium → high, xhigh → max; always enabled.",
                ),
            ),
            (
                "kimi-k2.6",
                "reasoning_effort",
                "low / medium / high / xhigh",
                choose(
                    "Без поля выключено; перечисленные значения включают. max не поддерживается.",
                    "Disabled when omitted; these values enable it. max is unsupported.",
                ),
            ),
            (
                "kimi-k2.7-code",
                "—",
                "—",
                choose(
                    "Всегда включено; thinking/reasoning_effort не задают уровень.",
                    "Always enabled; thinking/reasoning_effort do not set a level.",
                ),
            ),
            (
                "kimi-k3",
                "reasoning_effort",
                "low / high / max",
                choose(
                    "По умолчанию max; всегда включено. Сохраняйте один уровень на протяжении диалога.",
                    "Default max; always enabled. Keep one level throughout a conversation.",
                ),
            ),
        ],
    )
    result += _curl(EXAMPLES[11])

    result += '<h3 id="text-stream">SSE / stream=true</h3>'
    result += paragraph(
        "Успешный поток имеет Content-Type: text/event-stream и X-Request-Id. События разделяются пустой "
        "строкой; граница сетевого чанка не является границей события. Собирайте data: до конца события, "
        "затем разбирайте JSON. Обрабатывайте event:, служебные блоки и ошибки; при [DONE] JSON-парсер "
        "не вызывается. Иллюстрации ниже сокращены: токены usage и текст приведены только как пример.",
        "A successful stream has Content-Type: text/event-stream and X-Request-Id. Events end with a "
        "blank line; network chunk boundaries are not event boundaries. Collect data: lines to the "
        "end of an event before parsing JSON. Handle event: names, control blocks and errors; do not "
        "parse [DONE] as JSON. The shortened examples below use illustrative text and token counts.",
    )
    result += _code(
        "event: response.output_text.delta\n"
        'data: {"type":"response.output_text.delta","delta":"Ready."}\n\n'
        "event: response.completed\n"
        'data: {"type":"response.completed","response":{"id":"8488e6a4-d38a-4f95-a831-752aa12bf5d7",'
        '"status":"completed","usage":{"input_tokens":12,"output_tokens":3}}}\n\n'
    )
    result += paragraph(
        "Responses: собирайте response.output_text.delta.delta; конец — response.completed или "
        "response.incomplete. Последний означает частичный результат. Function-call arguments приходят "
        "отдельными событиями; их тоже нужно собрать до разбора JSON.",
        "Responses: accumulate response.output_text.delta.delta; completion events are response.completed "
        "or response.incomplete. The latter is partial output. Function-call arguments arrive in separate "
        "events and must also be assembled before JSON parsing.",
    )
    result += _code(
        'data: {"choices":[{"index":0,"delta":{"content":"Ready."},"finish_reason":null}]}\n\n'
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":3,"total_tokens":15}}\n\n'
        "data: [DONE]\n\n"
    )
    result += paragraph(
        "Chat: объединяйте choices[].delta по index; content и tool_calls могут приходить частями. "
        "Не заканчивайте чтение сразу на finish_reason: после него может идти usage с пустым choices.",
        "Chat: accumulate choices[].delta by index; content and tool_calls may be partial. Do not stop "
        "reading at finish_reason: a usage chunk with empty choices may follow.",
    )
    result += _code(
        "event: content_block_delta\n"
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Ready."}}\n\n'
        "event: message_delta\n"
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}\n\n'
        "event: message_stop\n"
        'data: {"type":"message_stop"}\n\n'
    )
    result += paragraph(
        "Messages: обработайте message_start, content_block_start/delta/stop, message_delta и "
        "message_stop; text_delta.text — текст, input_json_delta.partial_json — часть аргументов "
        "инструмента. Счётчики usage могут находиться в нескольких событиях.",
        "Messages: handle message_start, content_block_start/delta/stop, message_delta and message_stop. "
        "text_delta.text contains text; input_json_delta.partial_json contains partial tool arguments. "
        "Usage counters may arrive in multiple events.",
    )
    result += paragraph(
        "Сохраните X-Request-Id для обращения в поддержку. Разрыв потока или клиентский таймаут не "
        "означает отмену запроса: частичный текст не подтверждает успешное завершение. Не создавайте "
        "новую платную операцию автоматически при обрыве. Повтор синхронного запроса с тем же ключом "
        "возвращает 409 request_already_submitted, а не сохранённый текст. Описание ошибок — в общем разделе.",
        "Save X-Request-Id for support. A broken stream or client timeout does not cancel the request; "
        "partial text does not establish success. Do not automatically create a new paid operation "
        "after an interruption. Repeating a synchronous request with the same key returns "
        "409 request_already_submitted rather than replaying the text. See the shared error reference.",
    )
    return result + "</section>"
