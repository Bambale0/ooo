from decimal import Decimal

import pytest

from app.catalog.models import Model, PartnerPrice


async def test_root_is_a_working_public_entry(client):
    root = await client.get("/")
    docs = await client.get("/docs")
    assert root.status_code == 200
    assert root.text == docs.text


@pytest.mark.parametrize(
    "asset,mime",
    [
        ("brand.css", "text/css"),
        ("brand.js", "text/javascript"),
        ("logo-mark.svg", "image/svg+xml"),
        ("neuronych.webp", "image/webp"),
    ],
)
async def test_packaged_brand_assets_are_served_with_correct_type(client, asset, mime):
    response = await client.get(f"/ui/{asset}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(mime)
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.content


@pytest.mark.parametrize("asset", ["guide.py", "catalog.json", ".env", "%2e%2e%2fguide.py"])
async def test_public_assets_do_not_expose_other_files(client, asset):
    assert (await client.get(f"/ui/{asset}")).status_code == 404


async def test_empty_prices_explain_next_step(client):
    response = await client.get("/prices")
    assert response.status_code == 200
    assert "Приём заказов ещё не открыт" in response.text
    assert 'href="/docs">Открыть документацию' in response.text
    assert '<input id="price-search"' not in response.text


async def test_prices_keep_precision_visibility_and_escape_untrusted_names(client, db_session):
    public = Model(slug="visible", name='<script>alert("x")</script>', modality="image", status="production")
    draft = Model(slug="draft", name="PRIVATE_DRAFT_NAME", modality="image", status="draft")
    db_session.add_all([public, draft])
    await db_session.flush()
    for model in (public, draft):
        db_session.add(
            PartnerPrice(
                model_id=model.id,
                mode="standard",
                resolution="1024",
                price_rub=Decimal("1234.50"),
                provider_cost_usdt=Decimal("987.654321"),
                billing_unit="generation",
            )
        )
    await db_session.flush()
    body = (await client.get("/prices")).text
    assert "1234.50" in body
    assert "987.654321" not in body
    assert "PRIVATE_DRAFT_NAME" not in body
    assert '<script>alert("x")</script>' not in body
    assert "&lt;script&gt;" in body
    assert "<tbody>" in body  # The actual prices remain readable without JavaScript.
    assert 'id="no-results"' in body
