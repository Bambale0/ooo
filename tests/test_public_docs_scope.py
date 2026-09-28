from app.infrastructure.config import get_settings


async def test_public_docs_expose_only_model_connection_surface(client):
    response = await client.get("/docs")

    assert response.status_code == 200
    body = response.text
    assert get_settings().public_api_base_url.rstrip("/") in body
    assert "/v1/models" in body
    assert "Authorization: Bearer" in body

    forbidden = (
        "OpenAPI",
        "billing",
        "webhook",
        "retry",
        "ledger",
        "/internal/metrics",
        "/api/v1/providers",
        "/api/v1/billing",
    )
    for value in forbidden:
        assert value not in body


async def test_full_openapi_and_redoc_are_not_public(client):
    openapi = await client.get("/openapi.json")
    redoc = await client.get("/redoc")

    assert openapi.status_code == 404
    assert redoc.status_code == 404


async def test_guide_alias_uses_same_minimal_public_scope(client):
    docs = await client.get("/docs?lang=en")
    guide = await client.get("/guide?lang=en")

    assert docs.status_code == 200
    assert guide.status_code == 200
    assert docs.text == guide.text


async def test_docs_navigation_stays_in_configured_deployment(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "public_api_base_url", "https://api.example.org/preprod/")
    for route in ("/docs", "/guide", "/prices"):
        response = await client.get(route)
        assert response.status_code == 200
        assert 'href="/preprod/docs?lang=ru"' in response.text
        assert 'href="/preprod/docs?lang=en"' in response.text
        assert 'href="/docs?' not in response.text
