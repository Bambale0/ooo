import copy
import json
from argparse import Namespace
from decimal import Decimal

import httpx
import pytest

from app.contracts.registry import MODELS
from ops.smoke.argolink import catalog_matches, run, validate_budget, video_smoke_request


def live_catalog():
    saved = MODELS["seedance-2.5"]
    return {
        "revision": "new-unrelated-catalog-revision",
        "items": [
            {
                "id": "seedance-2.5",
                "category": saved["category"],
                "endpoint": saved["endpoint"],
                "pricing": {"effective": copy.deepcopy(saved["procurement"])},
            }
        ],
    }


def test_targeted_smoke_checks_selected_contract_despite_unrelated_revision_change():
    data = live_catalog()
    assert catalog_matches(data, ["seedance-2.5"])
    assert not catalog_matches(data, None)
    data["items"][0]["pricing"]["effective"]["tiers"][0]["price"] = Decimal(".09")
    assert not catalog_matches(data, ["seedance-2.5"])
    assert not catalog_matches({"items": []}, ["seedance-2.5"])


@pytest.mark.parametrize(
    "budget,usage",
    [
        ("NaN", {"mode": "unrestricted"}),
        ("Infinity", {"mode": "unrestricted"}),
        ("0", {"mode": "unrestricted"}),
        ("1", {"mode": "quota", "quota": {"remaining": ".5"}}),
        ("1", {"mode": "quota"}),
    ],
)
def test_invalid_or_unverifiable_budget_fails_closed(budget, usage):
    with pytest.raises(SystemExit):
        validate_budget(Decimal(budget), usage)


def test_unrestricted_key_still_requires_explicit_finite_budget():
    validate_budget(Decimal(".32"), {"mode": "unrestricted", "quota": None})
    body, estimate = video_smoke_request("seedance-2.5", "https://media.example/reference.jpg", "reference")
    assert body["reference_images"] == [{"url": "https://media.example/reference.jpg"}]
    assert "start_image" not in body and estimate == Decimal(".312")


@pytest.mark.parametrize("content_status,expected", [(206, "done"), (403, "content_unverified")])
async def test_reference_smoke_uploads_polls_and_checks_protected_content(
    tmp_path,
    monkeypatch,
    content_status,
    expected,
):
    secret, reference, report = tmp_path / "secret.json", tmp_path / "input.jpg", tmp_path / "report.json"
    secret.write_text(json.dumps({"argolink_key": "test-private-key"}))
    reference.write_bytes(b"jpeg-fixture")
    submissions = []

    def handler(request):
        path = request.url.path
        if path == "/v1/usage":
            return httpx.Response(200, json={"mode": "unrestricted", "usage": {"total": {"actual_cost": 0}}})
        if path == "/api/catalog/v1/models":
            return httpx.Response(200, json=json.loads(json.dumps(live_catalog(), default=float)))
        if path == "/v1/media/uploads":
            return httpx.Response(
                200,
                json={
                    "upload_url": "https://storage.example/upload",
                    "media_url": "https://media.example/private-photo",
                },
            )
        if path == "/upload":
            assert "authorization" not in request.headers
            assert request.content == b"jpeg-fixture"
            return httpx.Response(200)
        if path == "/v1/videos/generations":
            submissions.append(json.loads(request.content))
            return httpx.Response(202, json={"request_id": "private-job"})
        if path == "/v1/videos/private-job":
            return httpx.Response(200, json={"status": "done", "usage": {"billed_seconds": 4}})
        if path == "/v1/videos/private-job/content":
            assert request.headers["Range"] == "bytes=0-1023"
            assert request.headers["Authorization"] == "Bearer test-private-key"
            return httpx.Response(content_status, content=b"video-fixture", headers={"content-type": "video/mp4"})
        raise AssertionError(f"Unexpected request to {path}")

    client_type = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_type(transport=httpx.MockTransport(handler), **kw))
    args = Namespace(
        secret_file=str(secret),
        report=str(report),
        reference=str(reference),
        execute=True,
        budget_usd=Decimal(".32"),
        model=["seedance-2.5"],
        protocol=None,
        video_mode="reference",
    )
    if content_status == 206:
        await run(args)
    else:
        with pytest.raises(SystemExit):
            await run(args)
    public = report.read_text()
    assert json.loads(public)["results"][0]["result"] == expected
    assert len(submissions) == 1 and "reference_images" in submissions[0]
    assert all(value not in public for value in ("test-private-key", "private-job", "private-photo"))
    with pytest.raises(SystemExit, match="existing runs"):
        await run(args)
    assert len(submissions) == 1
