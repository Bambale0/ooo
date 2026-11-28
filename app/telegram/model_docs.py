"""Model documentation navigation, with authorization rechecked on every click."""

import asyncio
import logging

from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile

from app.api.model_integration import agent_instructions, documented_models, model_document, render_model_pdf
from app.infrastructure.config import get_settings
from app.telegram.ui import keyboard, show

logger = logging.getLogger(__name__)
_PAGE_SIZE = 8


async def model_docs_callback(event, db, data, partner_id):
    rows = await documented_models(db, partner_id)
    action, _, value = data.partition(":")
    if action == "docs_models":
        if not value.isascii() or not value.isdigit() or len(value) > 5:
            raise ValueError("invalid page")
        total = max(1, (len(rows) + _PAGE_SIZE - 1) // _PAGE_SIZE)
        page = min(int(value), total - 1)
        buttons = [(row.name, f"docs_model:{row.id}") for row in rows[
            page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE
        ]]
        if page:
            buttons.append(("← Предыдущие", f"docs_models:{page - 1}"))
        if page + 1 < total:
            buttons.append(("Следующие →", f"docs_models:{page + 1}"))
        await show(
            event, f"Документация по моделям · {page + 1}/{total}\n\n"
            "Выберите модель, чтобы получить PDF или инструкцию для ИИ."
            if rows else "Нет доступных моделей.",
            keyboard(*buttons, back="docs"),
        )
        return
    selected = next((row for row in rows if str(row.id) == value), None)
    if selected is None:
        await event.message.answer("Модель больше недоступна. Откройте актуальный список.", parse_mode=None)
        return
    if action == "docs_model":
        page = rows.index(selected) // _PAGE_SIZE
        await show(
            event, f"{selected.name}\nModel ID: {selected.slug}\n\nВыберите способ интеграции.",
            keyboard(
                ("📄 Ручная интеграция · PDF", f"docs_pdf:{selected.id}"),
                ("🤖 Инструкция для ИИ", f"docs_agent:{selected.id}"),
                back=f"docs_models:{page}",
            ),
        )
        return
    try:
        doc = model_document(selected.slug, selected.name, get_settings().public_api_base_url)
        if action == "docs_pdf":
            pdf = await asyncio.to_thread(render_model_pdf, doc)
            await event.message.answer_document(
                BufferedInputFile(pdf, filename=f"{doc.slug}-api.pdf"),
                caption=f"{doc.name} — документация API", parse_mode=None,
            )
        elif action == "docs_agent":
            prompt = agent_instructions(doc)
            if len(prompt.encode("utf-16-le")) // 2 <= 4000:
                await event.message.answer(prompt, parse_mode=None)
            else:
                await event.message.answer_document(
                    BufferedInputFile(prompt.encode("utf-8"), filename=f"{doc.slug}-agent.md"),
                    caption="Готовая инструкция для ИИ. Скопируйте содержимое файла в своего агента.",
                    parse_mode=None,
                )
    except (OSError, ValueError, TelegramAPIError) as exc:
        logger.warning("model_docs_delivery_failed", extra={
            "model_slug": selected.slug, "action": action, "error_type": type(exc).__name__,
        })
        await event.message.answer(
            "Не удалось подготовить или отправить документ. Попробуйте ещё раз.", parse_mode=None,
        )
