from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.model_integration import agent_instructions, model_document, render_model_pdf, render_model_selection
from app.contracts.registry import MODELS, validate_request
from app.telegram.model_docs import model_docs_callback


@pytest.mark.parametrize("slug", sorted(MODELS))
def test_every_contract_has_valid_model_specific_example(slug):
    doc = model_document(slug, slug, "https://api.example.com")
    assert doc.examples
    for protocol, body in doc.examples:
        assert body["model"] == slug
        assert validate_request(protocol, body) == body
    prompt = agent_instructions(doc)
    assert slug in prompt
    assert "Idempotency-Key" in prompt
    assert "NEIRONYCH_API_KEY" in prompt
    for secret in ("procurement", "provider_cost", "retail_markup", "ArgoLink"):
        assert secret not in prompt


def test_pdf_is_real_unicode_document():
    doc = model_document("seedance-2.5", "Seedance 2.5", "https://api.example.com")
    pdf = render_model_pdf(doc)
    assert pdf.startswith(b"%PDF-")
    assert b"/ToUnicode" in pdf
    assert len(pdf) > 5000


def test_selection_escapes_model_name_and_supports_both_modes():
    row = SimpleNamespace(slug="seedance-2.5", name="<script>alert(1)</script>", modality="video")
    html = render_model_selection([row], "https://api.example.com", "/docs?integration=1", row.slug)
    assert "<script>" not in html
    assert "data-copy-target" in html
    assert "format=pdf" in html
    assert "format=agent" in html


@pytest.mark.asyncio
async def test_stale_model_callback_does_not_send_document(monkeypatch):
    async def no_models(db, partner_id=None):
        return []

    monkeypatch.setattr("app.telegram.model_docs.documented_models", no_models)
    event = SimpleNamespace(message=SimpleNamespace(answer_document=AsyncMock(), answer=AsyncMock()))
    await model_docs_callback(event, None, "docs_pdf:00000000-0000-4000-8000-000000000001", "partner")
    event.message.answer_document.assert_not_awaited()
    event.message.answer.assert_awaited_once()
