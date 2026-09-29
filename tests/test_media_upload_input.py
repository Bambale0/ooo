import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import httpx
import pytest
from sqlalchemy import func, select

from app.accounts.models import ApiKey, Partner
from app.billing.models import LedgerEntry
from app.generations.models import Generation
from app.infrastructure.security import hash_secret
from app.providers.argolink import ArgoLinkAdapter
from app.providers.service import get_partner_provider_adapter


@pytest.fixture
async def upload_context(db_session, monkeypatch):
    partner = Partner(
        telegram_id="upload-input-partner",
        company_name="Upload input tests",
        project_name="Local test",
        balance_rub=Decimal("123.45"),
    )
    db_session.add(partner)
    await db_session.flush()
    db_session.add(
        ApiKey(
            partner_id=partner.id,
            name="upload-input-test",
            key_hash=hash_secret("upload-input-test-key"),
            key_prefix="test",
        )
    )
    await db_session.commit()
    adapter = create_autospec(ArgoLinkAdapter, instance=True, spec_set=True)
    lookup = AsyncMock(spec=get_partner_provider_adapter, return_value=adapter)
    monkeypatch.setattr("app.inference.router.get_partner_provider_adapter", lookup)
    return SimpleNamespace(
        partner=partner,
        headers={"Authorization": "Bearer upload-input-test-key"},
        adapter=adapter,
        lookup=lookup,
    )


async def assert_no_generation_or_charge(db, partner):
    await db.refresh(partner)
    assert partner.balance_rub == Decimal("123.45")
    assert await db.scalar(select(func.count()).select_from(Generation)) == 0
    assert await db.scalar(select(func.count()).select_from(LedgerEntry)) == 0


async def test_multipart_jpeg_returns_415_without_provider_call(client, db_session, upload_context):
    response = await client.post(
        "/v1/media/uploads",
        headers=upload_context.headers,
        data={"model": "seedance-2.5"},
        files={"file": ("reference.jpg", b"\xff\xd8\xff\xe0jpeg\xff\xd9", "image/jpeg")},
    )

    assert response.status_code == 415
    assert response.json() == {"detail": "media_upload_requires_json"}
    upload_context.lookup.assert_not_awaited()
    upload_context.adapter.native_request.assert_not_awaited()
    await assert_no_generation_or_charge(db_session, upload_context.partner)


async def test_invalid_utf8_json_returns_422_without_provider_call(client, db_session, upload_context):
    response = await client.post(
        "/v1/media/uploads",
        headers={**upload_context.headers, "Content-Type": "application/json"},
        content=b'{"model":"seedance-2.5","filename":"\xff.jpg"}',
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_request_contract"}
    upload_context.lookup.assert_not_awaited()
    upload_context.adapter.native_request.assert_not_awaited()
    await assert_no_generation_or_charge(db_session, upload_context.partner)


@pytest.mark.parametrize("content_type", ["image/jpeg", "application/octet-stream", "text/plain"])
async def test_non_json_content_type_returns_415(client, db_session, upload_context, content_type):
    response = await client.post(
        "/v1/media/uploads",
        headers={**upload_context.headers, "Content-Type": content_type},
        content=b'{"model":"seedance-2.5"}',
    )

    assert response.status_code == 415
    assert response.json() == {"detail": "media_upload_requires_json"}
    upload_context.lookup.assert_not_awaited()
    upload_context.adapter.native_request.assert_not_awaited()
    await assert_no_generation_or_charge(db_session, upload_context.partner)


@pytest.mark.parametrize("body", [b"", b'{"model":', b'{"model":"seedance-2.5"} trailing'])
async def test_malformed_json_returns_422_without_provider_call(client, db_session, upload_context, body):
    response = await client.post(
        "/v1/media/uploads",
        headers={**upload_context.headers, "Content-Type": "application/json"},
        content=body,
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_request_contract"}
    upload_context.lookup.assert_not_awaited()
    upload_context.adapter.native_request.assert_not_awaited()
    await assert_no_generation_or_charge(db_session, upload_context.partner)


@pytest.mark.parametrize(
    "body",
    [None, [], "seedance-2.5", {}, {"model": "unknown"}, {"model": []}, {"model": {}}, {"model": None}],
    ids=["null", "array", "string", "missing-model", "unknown-model", "array-model", "object-model", "null-model"],
)
async def test_invalid_model_contract_returns_422_without_provider_call(client, db_session, upload_context, body):
    response = await client.post(
        "/v1/media/uploads",
        headers={**upload_context.headers, "Content-Type": "application/json"},
        content=json.dumps(body).encode(),
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "unknown_model_contract"}
    upload_context.lookup.assert_not_awaited()
    upload_context.adapter.native_request.assert_not_awaited()
    await assert_no_generation_or_charge(db_session, upload_context.partner)


@pytest.mark.parametrize("content_type", ["multipart/form-data; boundary=test", "application/json"])
async def test_unauthenticated_upload_returns_401_before_parsing(client, db_session, upload_context, content_type):
    response = await client.post(
        "/v1/media/uploads",
        headers={"Content-Type": content_type},
        content=b"\xff\xd8\xff\xe0",
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "api_key_required"}
    upload_context.lookup.assert_not_awaited()
    upload_context.adapter.native_request.assert_not_awaited()
    await assert_no_generation_or_charge(db_session, upload_context.partner)


@pytest.mark.parametrize("content_type", ["application/json", "application/json; charset=utf-8", None])
async def test_json_upload_returns_201_ticket_without_charging(client, db_session, upload_context, content_type):
    body = {
        "model": "seedance-2.5",
        "type": "image",
        "content_type": "image/jpeg",
        "size_bytes": 482632,
    }
    ticket = {
        "upload_url": "https://storage.example.org/upload?signature=test",
        "media_url": "https://storage.example.org/reference.jpg?signature=test",
        "upload_expires_at": "2026-09-29T12:00:00Z",
        "expires_at": "2026-09-30T12:00:00Z",
    }
    upload_context.adapter.native_request.return_value = httpx.Response(
        201, json={**ticket, "provider": "private-provider", "request_id": "private-ticket-id"}
    )
    headers = dict(upload_context.headers)
    if content_type is not None:
        headers["Content-Type"] = content_type

    response = await client.post("/v1/media/uploads", headers=headers, content=json.dumps(body).encode())

    assert response.status_code == 201
    assert response.json() == ticket
    upload_context.lookup.assert_awaited_once_with(db_session, upload_context.partner.id, "argolink")
    upload_context.adapter.native_request.assert_awaited_once_with("media/uploads", body)
    await assert_no_generation_or_charge(db_session, upload_context.partner)
