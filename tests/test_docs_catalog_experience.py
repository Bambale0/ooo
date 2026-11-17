"""Navigation and public catalog regressions: availability, exact prices and safe docs links."""

from decimal import Decimal

import pytest

from app.accounts.models import Partner
from app.catalog.models import Model, PartnerPrice
from app.dashboard.portal_ui import page_html


@pytest.mark.parametrize("language", ("ru", "en"))
async def test_documentation_is_a_navigable_developer_hub(client, language):
    response = await client.get("/docs", params={"lang": language})
    assert response.status_code == 200
    assert 'class="docs-page"' in response.text
    assert 'id="docs-nav-search"' in response.text
    assert 'id="public-api-base"' in response.text
    assert 'data-copy-target="public-api-base"' in response.text
    for anchor in ("connect", "models", "video", "errors"):
        assert f'href="#{anchor}"' in response.text
        assert f'id="{anchor}"' in response.text
    assert "/v1/models" in response.text
    assert "/v1/v1/" not in response.text
    assert "procurement" not in response.text.lower()


def test_portal_navigation_has_visible_docs_and_catalog_actions():
    partner = Partner(
        company_name="Example", project_name="Demo", telegram_id="123456",
        status="active", balance_rub=Decimal("100.00"),
    )
    response = page_html(partner, "overview", "<p>Safe</p>", "test-csrf")
    assert 'href="/guide?lang=ru"' in response.body.decode()
    assert 'href="/prices"' in response.body.decode()
    assert "Документация" in response.body.decode()
    assert 'class="topbar-actions"' in response.body.decode()


async def test_catalog_is_browsable_without_js_and_never_leaks_draft(client, db_session):
    public = Model(
        slug="gpt-6-astra", name="Example text model",
        modality="llm", status="production",
    )
    private = Model(
        slug="private-incomplete", name="PRIVATE_INTERNAL_MODEL", modality="llm", status="draft",
    )
    restricted = Model(
        slug="restricted-model", name="RESTRICTED_INTERNAL_MODEL",
        modality="video", status="restricted",
    )
    injected = Model(
        slug="public-special", name="<script>alert('x')</script>",
        modality="image", status="production",
    )
    db_session.add_all([public, private, restricted, injected])
    await db_session.flush()
    for model, amount in (
        (public, Decimal("9.55")),
        (private, Decimal("88888.00")),
        (restricted, Decimal("44444.00")),
        (injected, Decimal("25.00")),
    ):
        db_session.add(
            PartnerPrice(
                model_id=model.id, mode="default", resolution="default",
                price_rub=amount, billing_unit="million_tokens",
                provider_cost_usdt=Decimal("999.99"),
            )
        )
    await db_session.flush()
    response = await client.get("/models")
    assert response.status_code == 200
    body = response.text
    assert "Example text model" in body
    assert "gpt-6-astra" in body
    assert "9.55" in body
    assert "/v1/responses" in body
    assert "/v1/v1/" not in body
    assert "PRIVATE_INTERNAL_MODEL" not in body
    assert "RESTRICTED_INTERNAL_MODEL" not in body
    assert "88888.00" not in body
    assert "999.99" not in body
    assert '<script>alert(' not in body
    assert "&lt;script&gt;" in body
    assert 'id="price-search"' in body
    assert 'id="price-sort"' in body
    assert 'data-kind="text"' in body
    assert 'data-kind="image"' in body
    assert 'id="no-results"' in body
    assert "<tbody>" in body
    assert (await client.get("/prices")).text == body
    assert (await client.get("/price")).text == body


async def test_public_model_reference_keeps_exact_contract_endpoint(client, db_session):
    model = Model(
        slug="gpt-6-astra", name="GPT Astra Model",
        modality="llm", status="production",
    )
    db_session.add(model)
    await db_session.flush()
    html = (await client.get("/docs")).text
    assert "GPT Astra Model" in html
    assert "/v1/responses" in html
    assert "/v1/v1/responses" not in html


async def test_public_catalog_empty_state_has_fallback_for_no_javascript(client):
    response = await client.get("/models")
    assert response.status_code == 200
    assert "Приём заказов ещё не открыт" in response.text
    assert 'href="/docs">Открыть документацию' in response.text
    assert 'id="price-search"' not in response.text
