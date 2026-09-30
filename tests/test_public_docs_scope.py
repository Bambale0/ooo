import ast
import copy
import json
from html.parser import HTMLParser
from importlib.resources import files

import pytest

from app.api.partner_reference import EXAMPLES, VIDEO_LIMITS
from app.api.text_reference import EXAMPLES as TEXT_EXAMPLES
from app.api.text_reference import PUBLIC_TEXT_MODELS
from app.catalog.models import Model
from app.contracts.registry import MODELS, validate_request
from app.infrastructure.config import get_settings


class ReferenceHTML(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.ids, self.links, self.code = set(), [], []
        self.in_code = False
        self.current = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        if tag == "a":
            self.links.append(attrs["href"])
        if tag == "code":
            self.in_code = True
            self.current = []

    def handle_data(self, data):
        if self.in_code:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == "code":
            self.in_code = False
            self.code.append("".join(self.current))


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_public_docs_are_self_contained_without_private_surface(client, lang):
    response = await client.get("/docs", params={"lang": lang})

    assert response.status_code == 200
    body = response.text
    assert get_settings().public_api_base_url.rstrip("/") in body
    assert "/v1/models" in body
    assert "Authorization: Bearer" in body

    for term in (
        "Idempotency-Key",
        "reference_images",
        "omni_reference_task_type",
        "adaptive",
        "billed_seconds",
        "/v1/media/uploads",
        "/v1/videos/{request_id}/content",
        "seedance_reference.py",
        "max_output_tokens",
        "response_format",
        "text/event-stream",
        "invalid_request_contract",
        "request_already_submitted",
        'id="text-luna"',
        'id="text-glm-flash"',
        "service_tier",
        "priority",
    ):
        assert term in body
    for value in (
        "argolink",
        "luckyapi",
        "procurement",
        "provider_task_id",
        "provider_cost",
        "webhook",
        "ledger",
        "reconciliation",
        "5ce662ba88c3d07f",
        "/internal/metrics",
        "/api/v1/providers",
        "/api/v1/billing",
    ):
        assert value not in body.lower()
    parsed = ReferenceHTML(body)
    assert all(link[1:] in parsed.ids for link in parsed.links if link.startswith("#"))
    # The pricing link deliberately targets the configured API host: the docs
    # host must not proxy pricing or other account/internal endpoints.
    pricing_url = get_settings().public_api_base_url.rstrip("/") + "/prices"
    assert all(not link.startswith("http") or link == pricing_url for link in parsed.links)

    # Test the exact executable source customers copy, including HTML round-trip.
    script = files("app.api.examples").joinpath("seedance_reference.py").read_text()
    assert script in parsed.code
    ast.parse(script)
    rendered_requests = []
    for block in parsed.code:
        if block.startswith("{"):
            value = json.loads(block)
            if "model" in value and "size_bytes" not in value:
                rendered_requests.append(value)
    assert all(body in rendered_requests for _, body in EXAMPLES.values())


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_docs_explain_luna_fast_cost_and_glm_conversion_limits(client, lang):
    response = await client.get("/docs", params={"lang": lang})
    body = response.text
    luna = body.split('id="text-luna"', 1)[1].split('id="text-glm-flash"', 1)[0]
    glm = body.split('id="text-glm-flash"', 1)[1].split("REQUEST_KEY=", 1)[0]
    assert all(word in luna for word in ("gpt-5.6-luna", "priority", "fast", "input_image", "reasoning.effort"))
    assert ("умножаются на 2" if lang == "ru" else "multiplied by 2") in luna
    assert all(word in glm for word in ("glm-5.3-flash", "input_file", "base64", "output_config.effort"))


@pytest.mark.parametrize(
    "protocol,body", list(EXAMPLES.values()) + [(item["protocol"], item["body"]) for item in TEXT_EXAMPLES]
)
def test_published_request_examples_match_runtime_contract(protocol, body):
    original = copy.deepcopy(body)
    assert validate_request(protocol, body) == original
    assert body == original


@pytest.mark.parametrize("model,seconds,resolutions,refs", VIDEO_LIMITS)
def test_published_video_duration_and_resolution_limits_match_contract(model, seconds, resolutions, refs):
    minimum, maximum = map(int, seconds.split("–"))
    for resolution in resolutions.split(", "):
        for duration in (minimum, maximum):
            validate_request(
                "videos/generations",
                {
                    "model": model,
                    "prompt": "A slow camera move",
                    "duration": duration,
                    "resolution": resolution,
                },
            )
    for duration in (minimum - 1, maximum + 1):
        with pytest.raises(ValueError, match="invalid_duration"):
            validate_request("videos/generations", {"model": model, "prompt": "A camera move", "duration": duration})


def test_video_table_covers_every_supported_video_model():
    assert {row[0] for row in VIDEO_LIMITS} == {name for name, item in MODELS.items() if item["category"] == "video"}


def test_text_metadata_is_explicitly_public_and_covers_supported_ids():
    assert set(PUBLIC_TEXT_MODELS) == {name for name, item in MODELS.items() if item["category"] == "chat"}
    allowed = {
        "context_tokens",
        "max_input_tokens",
        "max_output_tokens",
        "default_output_tokens",
        "images",
        "efforts",
        "effort_aliases",
        "default_effort",
        "thinking",
    }
    for metadata in PUBLIC_TEXT_MODELS.values():
        assert set(metadata) <= allowed
        for key in ("context_tokens", "max_input_tokens", "max_output_tokens", "default_output_tokens"):
            if key in metadata:
                assert isinstance(metadata[key], int) and metadata[key] > 0


async def test_enabled_docs_models_match_discovery_and_escape_names(client, db_session):
    db_session.add_all(
        [
            Model(slug="seedance-2.5", name='<script>alert("name")</script>', modality="video", status="production"),
            Model(slug="wan-3", name="DRAFT_MODEL_NOT_PUBLIC", modality="video", status="draft"),
            Model(slug="unknown-contract", name="UNKNOWN_MODEL_NOT_PUBLIC", modality="video", status="production"),
        ]
    )
    await db_session.flush()
    response = await client.get("/docs")
    discovery = await client.get("/v1/models")
    assert [item["id"] for item in discovery.json()["data"]] == ["seedance-2.5"]
    assert '<script>alert("name")</script>' not in response.text
    assert "&lt;script&gt;alert(&quot;name&quot;)&lt;/script&gt;" in response.text
    assert "DRAFT_MODEL_NOT_PUBLIC" not in response.text
    assert "UNKNOWN_MODEL_NOT_PUBLIC" not in response.text


async def test_full_openapi_and_redoc_are_not_public(client):
    openapi = await client.get("/openapi.json")
    redoc = await client.get("/redoc")

    assert openapi.status_code == 404
    assert redoc.status_code == 404


async def test_guide_alias_uses_same_public_reference(client):
    docs = await client.get("/docs?lang=en")
    guide = await client.get("/guide?lang=en")

    assert docs.status_code == 200
    assert guide.status_code == 200
    assert docs.text == guide.text
