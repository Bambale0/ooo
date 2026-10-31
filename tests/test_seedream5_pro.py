"""Seedream contract, actual-only billing and retry regression coverage."""

import base64
import io
import json
from decimal import Decimal

import httpx
import pytest
from PIL import Image
from sqlalchemy import select
from test_native_inference import setup

from app.billing.models import LedgerEntry
from app.contracts.registry import MODELS, image_reference_limit, image_tier, validate_request
from app.generations.models import Generation

MODEL = "seedream-5.0-pro"
RATIOS = ("1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9", "2:1", "1:2", "9:21")


def rendered(width=1984, height=None):
    out = io.BytesIO()
    Image.new("RGB", (width, height or width)).save(out, "JPEG")
    return {"b64_json": base64.b64encode(out.getvalue()).decode()}


def test_reviewed_seedream_contract():
    assert MODEL in MODELS
    entry = MODELS[MODEL]
    assert entry["category"] == "image"
    assert {t["label"]: Decimal(str(t["price"])) for t in entry["procurement"]["tiers"]} == {
        "1K": Decimal(".03"),
        "2K": Decimal(".03"),
    }
    assert image_reference_limit(MODEL) == image_reference_limit(MODEL, multipart=True) == 8
    assert image_tier({"model": MODEL}) == "2K"
    assert entry["image"]["max_reference_mb"] == 48
    assert entry["image"]["output_format"] == "JPEG"
    assert tuple(entry["image"]["aspect_ratios"]) == RATIOS


@pytest.mark.parametrize("protocol", ["images/generations", "images/edits"])
@pytest.mark.parametrize("resolution", ["1k", "2k", "1K", "2K"])
@pytest.mark.parametrize("ratio", RATIOS)
def test_documented_seedream_options_are_preserved(protocol, resolution, ratio):
    body = {"model": MODEL, "prompt": "A ceramic cup", "resolution": resolution, "aspect_ratio": ratio, "n": 1}
    if protocol.endswith("edits"):
        body["images"] = [{"image_url": f"https://example.org/{i}.png"} for i in range(8)]
    assert validate_request(protocol, body) == body


@pytest.mark.parametrize(
    "controls",
    [
        {"n": 2},
        {"n": True},
        {"n": 0},
        {"resolution": "4k"},
        {"resolution": "3k"},
        {"aspect_ratio": "2:1"},
        {"response_format": "jpeg"},
        {"size": "2048x2048"},
        {"stream": True},
        {"quality": "high"},
        {"images": [{}]},
        {"images": [{"image_url": "http://example.org/x.png"}]},
        {"images": [{"image_url": "https://example.org/x.png"}] * 9},
    ],
)
def test_unsupported_seedream_controls_are_rejected(controls):
    with pytest.raises(ValueError):
        validate_request("images/generations", {"model": MODEL, "prompt": "Cup", **controls})


def test_seedream_data_reference_validation():
    body = {
        "model": MODEL,
        "prompt": "Recolor",
        "images": [{"image_url": "data:image/jpeg;base64," + rendered()["b64_json"]}],
    }
    assert validate_request("images/edits", body) == body
    body["images"][0]["image_url"] = "data:image/png;base64,not-base64"
    with pytest.raises(ValueError):
        validate_request("images/edits", body)


@pytest.mark.parametrize("resolution,expected", [(None, "2K"), ("1k", "1K"), ("2k", "2K")])
async def test_seedream_charge_matches_tier_and_replay_is_free(client, db_session, monkeypatch, resolution, expected):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        edge = 1088 if calls[-1].get("resolution", "2k").lower() == "1k" else 1984
        return httpx.Response(200, json={"data": [rendered(edge)]})

    rates = [
        ("default", "1K", "generation", Decimal("3.85"), Decimal(".03")),
        ("default", "2K", "generation", Decimal("4.85"), Decimal(".03")),
    ]
    partner, headers, upstream = await setup(
        db_session, monkeypatch, handler, model=MODEL, category="image", rates=rates
    )
    body = {"model": MODEL, "prompt": "Cup", "n": 1}
    if resolution is not None:
        body["resolution"] = resolution
    try:
        response = await client.post("/v1/images/generations", headers=headers, json=body)
        assert response.status_code == 200, response.text
        replay = await client.post("/v1/images/generations", headers=headers, json=body)
        assert replay.status_code == 200 and replay.content == response.content
        g = await db_session.scalar(select(Generation))
        assert g.resolution == expected and g.usage_snapshot == {expected: 1}
        charge = Decimal("3.85") if expected == "1K" else Decimal("4.85")
        assert g.actual_charge_rub == charge and g.actual_provider_cost_usdt == Decimal(".03")
        assert partner.balance_rub == Decimal("1000000") - charge
        assert calls == [body]
    finally:
        await upstream.aclose()


@pytest.mark.parametrize("returned", [{"data": []}, {"data": [{}]}, {"data": [None]}, {"data": [{"b64_json": ""}]}])
async def test_empty_seedream_result_is_not_charged(client, db_session, monkeypatch, returned):
    rates = [("default", tier, "generation", Decimal("3.85"), Decimal(".03")) for tier in ("1K", "2K")]
    partner, headers, upstream = await setup(
        db_session,
        monkeypatch,
        lambda req: httpx.Response(200, json=returned),
        model=MODEL,
        category="image",
        rates=rates,
    )
    try:
        response = await client.post("/v1/images/generations", headers=headers, json={"model": MODEL, "prompt": "Cup"})
        assert response.status_code == 503
        g = await db_session.scalar(select(Generation))
        assert g.status == "failed" and g.billing_status == "not_chargeable" and g.actual_charge_rub is None
        assert partner.balance_rub == Decimal("1000000")
        assert not list(await db_session.scalars(select(LedgerEntry)))
    finally:
        await upstream.aclose()


@pytest.mark.parametrize("count,status", [(1, 200), (8, 200), (9, 422)])
async def test_seedream_multipart_edit_reference_limit(client, db_session, monkeypatch, count, status):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
        assert request.content.count(b'name="image[]"') == count
        return httpx.Response(200, json={"data": [rendered()]})

    rates = [("default", tier, "generation", Decimal("3.85"), Decimal(".03")) for tier in ("1K", "2K")]
    partner, headers, upstream = await setup(
        db_session, monkeypatch, handler, model=MODEL, category="image", rates=rates
    )
    try:
        data = {"model": MODEL, "prompt": "Recolor", "resolution": "2k", "n": "1"}
        files = [
            ("image[]", (str(i) + ".jpg", base64.b64decode(rendered()["b64_json"]), "image/jpeg"))
            for i in range(count)
        ]
        response = await client.post("/v1/images/edits", headers=headers, data=data, files=files)
        assert response.status_code == status, response.text
        assert len(calls) == (1 if status == 200 else 0)
    finally:
        await upstream.aclose()


async def test_seedream_url_result_is_validated_before_billing(client, db_session, monkeypatch):
    from app.infrastructure import public_http

    raw = base64.b64decode(rendered()["b64_json"])
    reads = []

    def content(request):
        reads.append(request.url)
        return httpx.Response(200, content=raw, headers={"Content-Type": "image/png"})

    monkeypatch.setattr(
        public_http, "public_http_client", lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(content))
    )
    rates = [("default", tier, "generation", Decimal("3.85"), Decimal(".03")) for tier in ("1K", "2K")]
    partner, headers, upstream = await setup(
        db_session,
        monkeypatch,
        lambda req: httpx.Response(200, json={"data": [{"url": "https://media.example.org/result.png"}]}),
        model=MODEL,
        category="image",
        rates=rates,
    )
    try:
        r = await client.post(
            "/v1/images/generations", headers=headers, json={"model": MODEL, "prompt": "Cup", "response_format": "url"}
        )
        assert r.status_code == 200, r.text
        assert r.json()["data"][0]["url"] == "https://media.example.org/result.png"
        assert len(reads) == 1
        g = await db_session.scalar(select(Generation))
        assert g.actual_charge_rub == Decimal("3.85") and g.usage_snapshot == {"2K": 1}
    finally:
        await upstream.aclose()


async def test_seedream_error_replay_never_submits_again(client, db_session, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, json={"error": "private upstream failure"})

    rates = [("default", tier, "generation", Decimal("3.85"), Decimal(".03")) for tier in ("1K", "2K")]
    partner, headers, upstream = await setup(
        db_session, monkeypatch, handler, model=MODEL, category="image", rates=rates
    )
    body = {"model": MODEL, "prompt": "Cup"}
    try:
        a = await client.post("/v1/images/generations", headers=headers, json=body)
        b = await client.post("/v1/images/generations", headers=headers, json=body)
        assert a.status_code == b.status_code == 503 and a.content == b.content
        assert len(calls) == 1 and partner.balance_rub == Decimal("1000000")
        assert "private upstream failure" not in a.text
    finally:
        await upstream.aclose()


def test_seedream_aggregate_inline_reference_limit(monkeypatch):
    profile = dict(MODELS[MODEL]["image"], max_reference_mb=1)
    monkeypatch.setitem(MODELS[MODEL], "image", profile)
    reference = "data:image/png;base64," + base64.b64encode(b"x" * (600 * 1024)).decode()
    with pytest.raises(ValueError, match="reference_limit_exceeded"):
        validate_request("images/edits", {"model": MODEL, "prompt": "Cup", "images": [{"image_url": reference}] * 2})


async def test_seedream_aggregate_multipart_limit_precedes_admission(client, db_session, monkeypatch):
    profile = dict(MODELS[MODEL]["image"], max_reference_mb=1)
    monkeypatch.setitem(MODELS[MODEL], "image", profile)
    rates = [("default", tier, "generation", Decimal("3.85"), Decimal(".03")) for tier in ("1K", "2K")]

    def no_submit(request):
        raise AssertionError("Oversized refs must not reach provider")

    partner, headers, upstream = await setup(
        db_session, monkeypatch, no_submit, model=MODEL, category="image", rates=rates
    )
    try:
        r = await client.post(
            "/v1/images/edits",
            headers=headers,
            data={"model": MODEL, "prompt": "Cup"},
            files=[("image[]", (str(i) + ".png", b"x" * (600 * 1024), "image/png")) for i in range(2)],
        )
        assert r.status_code == 422 and r.headers["X-Validation-Error"] == "reference_limit_exceeded"
        assert not list(await db_session.scalars(select(Generation)))
    finally:
        await upstream.aclose()


def test_seedream_default_does_not_change_existing_image_models():
    for model in ("nano-banana-pro", "nano-banana-2.1", "gpt-image-2"):
        assert image_tier({"model": model}) == "1K"


def test_seedream_wide_1k_output_is_not_misbilled_as_2k():
    from app.inference.images import image_usage

    out = io.BytesIO()
    Image.new("RGB", (1472, 832)).save(out, "JPEG")
    item = {"b64_json": base64.b64encode(out.getvalue()).decode()}
    assert image_usage({"model": MODEL, "resolution": "1k", "aspect_ratio": "16:9"}, {"data": [item]}) == {"1K": 1}


def test_seedream_wrong_output_dimensions_cannot_be_billed():
    from app.inference.images import image_usage

    with pytest.raises(ValueError, match="image_result_invalid_dimensions"):
        image_usage({"model": MODEL, "resolution": "2k"}, {"data": [rendered(1088)]})
