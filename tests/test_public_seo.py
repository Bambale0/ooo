import json
import re
from decimal import Decimal
from xml.etree import ElementTree

import pytest

from app.catalog.models import Model, PartnerPrice
from app.infrastructure.config import get_settings


@pytest.mark.parametrize(
    "path,canonical",
    [
        ("/", "/"),
        ("/prices", "/models"),
        ("/price", "/models"),
        ("/models", "/models"),
        ("/guide?lang=en", "/docs?lang=en"),
        ("/docs?lang=ru", "/docs"),
    ],
)
async def test_public_metadata(client, path, canonical):
    response = await client.get(path)
    base = get_settings().public_api_base_url.rstrip("/")
    assert response.status_code == 200
    assert f'<link rel="canonical" href="{base}{canonical}">' in response.text
    assert 'property="og:title"' in response.text
    assert 'name="twitter:card"' in response.text
    assert 'name="robots" content="index,follow"' in response.text
    assert "X-Robots-Tag" not in response.headers
    for payload in re.findall(r'<script type="application/ld\+json">(.*?)</script>', response.text):
        assert json.loads(payload)["@context"] == "https://schema.org"


async def test_sitemap_and_detail_publish_only_priced_contracts(client, db_session):
    for slug, status, priced in [
        ("seedance-2.0", "production", True),
        ("seedance-2.5", "draft", True),
        ("gpt-image-2", "production", False),
        ("fake-model", "production", True),
    ]:
        model = Model(slug=slug, name=slug, modality="video", status=status)
        db_session.add(model)
        await db_session.flush()
        if priced:
            db_session.add(
                PartnerPrice(
                    model_id=model.id,
                    mode="default",
                    resolution="720p",
                    price_rub=Decimal("12.34"),
                    billing_unit="second",
                    provider_cost_usdt=Decimal("987.65"),
                )
            )
    await db_session.flush()
    sitemap = await client.get("/sitemap.xml")
    assert sitemap.status_code == 200
    urls = [
        node.text
        for node in ElementTree.fromstring(sitemap.content).iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
    ]
    base = get_settings().public_api_base_url.rstrip("/")
    assert base + "/models/seedance-2.0" in urls
    assert not any(url.endswith(("/seedance-2.5", "/gpt-image-2", "/fake-model", "/prices", "/guide")) for url in urls)
    page = await client.get("/models/seedance-2.0")
    assert page.status_code == 200
    assert "12.34" in page.text and "987.65" not in page.text
    assert 'href="/models/seedance-2.0"' in (await client.get("/models")).text
    assert 'href="/models/video"' in (await client.get("/models")).text
    assert (await client.get("/models/video")).status_code == 200
    for slug in ["seedance-2.5", "gpt-image-2", "fake-model"]:
        response = await client.get("/models/" + slug)
        assert response.status_code == 404
        assert "noindex" in response.headers["X-Robots-Tag"]


async def test_crawler_controls(client):
    robots = await client.get("/robots.txt")
    assert robots.status_code == 200
    assert "Sitemap: " + get_settings().public_api_base_url.rstrip("/") + "/sitemap.xml" in robots.text
    assert "Disallow: /cabinet" not in robots.text  # allow crawling noindex, auth remains mandatory
    for path in ["/cabinet", "/v1/models", "/nonexistent-seo"]:
        assert "noindex" in (await client.get(path)).headers["X-Robots-Tag"]


async def test_partner_program_is_public_indexable_and_linked(client):
    from app.referrals.service import API_TOPUP_RATE, DEVELOPMENT_ORDER_RATE, rate_percent

    response = await client.get("/partners")
    assert response.status_code == 200
    base = get_settings().public_api_base_url.rstrip("/")
    assert f'<link rel="canonical" href="{base}/partners">' in response.text
    assert "X-Robots-Tag" not in response.headers
    assert rate_percent(API_TOPUP_RATE) in response.text
    assert rate_percent(DEVELOPMENT_ORDER_RATE) in response.text
    assert "/cabinet?section=referrals" in response.text
    assert 'src="/ui/neuronych.webp"' in response.text
    assert 'href="/partners"' in (await client.get("/")).text
    assert 'href="/partners"' in (await client.get("/prices")).text
    assert 'href="/partners"' not in (await client.get("/docs")).text
    assert f"<loc>{base}/partners</loc>" in (await client.get("/sitemap.xml")).text
