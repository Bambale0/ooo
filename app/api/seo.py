"""Server-rendered discovery metadata, using the configured public API origin."""

import json
from html import escape
from urllib.parse import quote
from xml.sax.saxutils import escape as xml_escape

from app.infrastructure.config import get_settings


def canonical_url(path: str) -> str:
    return get_settings().public_api_base_url.rstrip("/") + path


def model_path(slug: str) -> str:
    return "/models/" + quote(slug, safe="")


def seo_head(title: str, description: str, path: str, lang: str) -> str:
    url = canonical_url(path + ("?lang=en" if lang == "en" and path in {"/", "/docs"} else ""))
    image = canonical_url("/ui/neuronych.webp")
    esc = lambda value: escape(value, quote=True)  # noqa: E731
    tags = [f'<link rel="canonical" href="{esc(url)}">', '<meta name="robots" content="index,follow">']
    for key, value in {
        "og:type": "website",
        "og:title": title,
        "og:description": description,
        "og:url": url,
        "og:site_name": "Нейроныч API",
        "og:image": image,
        "og:locale": "ru_RU" if lang == "ru" else "en_US",
    }.items():
        tags.append(f'<meta property="{key}" content="{esc(value)}">')
    for key, value in {
        "twitter:card": "summary",
        "twitter:title": title,
        "twitter:description": description,
        "twitter:image": image,
    }.items():
        tags.append(f'<meta name="{key}" content="{esc(value)}">')
    if path in {"/", "/docs"}:
        for locale, suffix in [("ru", ""), ("en", "?lang=en"), ("x-default", "")]:
            tags.append(f'<link rel="alternate" hreflang="{locale}" href="{esc(canonical_url(path + suffix))}">')
    website = {"@type": "WebSite", "@id": canonical_url("/#website"), "url": canonical_url("/"), "name": "Нейроныч API"}
    schema = {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "@id": url + "#webpage",
        "url": url,
        "name": title,
        "description": description,
        "inLanguage": lang,
        "isPartOf": website,
    }
    if path == "/":
        schema = {
            "@context": "https://schema.org",
            "@graph": [website, {k: v for k, v in schema.items() if k != "@context"}],
        }
    elif path.startswith("/models/"):
        schema["breadcrumb"] = {
            "@type": "BreadcrumbList",
            "itemListElement": [
                {"@type": "ListItem", "position": i, "name": name, "item": item}
                for i, (name, item) in enumerate(
                    [
                        ("Нейроныч API", canonical_url("/")),
                        ("Модели", canonical_url("/models")),
                        (title.split(" | ")[0], url),
                    ],
                    1,
                )
            ],
        }
    payload = (
        json.dumps(schema, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    )
    tags.append(f'<script type="application/ld+json">{payload}</script>')
    return "".join(tags)


def sitemap_xml(paths: list[str]) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(f"<url><loc>{xml_escape(canonical_url(path))}</loc></url>" for path in dict.fromkeys(paths))
        + "</urlset>"
    )
