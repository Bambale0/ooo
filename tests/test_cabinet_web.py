"""Behavioral regression checks for Telegram-linked, tenant-isolated web cabinet."""

import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.accounts.models import ApiKey, Partner
from app.cabinet.security import verify_telegram_login
from app.infrastructure.config import get_settings
from app.infrastructure.security import hash_secret


def _telegram_login_data(token: str, telegram_id: str, now: datetime) -> dict[str, str]:
    payload = {
        "id": telegram_id,
        "first_name": "Test",
        "auth_date": str(int(now.timestamp())),
    }
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()))
    secret = hashlib.sha256(token.encode("utf-8")).digest()
    payload["hash"] = hmac.new(secret, data_check.encode("utf-8"), hashlib.sha256).hexdigest()
    return payload


def test_telegram_login_accepts_only_fresh_signed_identity(monkeypatch):
    token = "bot:test-secret-only"
    monkeypatch.setattr(get_settings(), "telegram_bot_token", token)
    now = datetime.now(UTC)
    valid = _telegram_login_data(token, "12345678", now)
    assert verify_telegram_login(valid, now=now) == "12345678"

    fake = {**valid, "id": "12345679"}
    with pytest.raises(HTTPException) as invalid:
        verify_telegram_login(fake, now=now)
    assert invalid.value.status_code == 401

    replay = _telegram_login_data(token, "12345678", now - timedelta(minutes=6))
    with pytest.raises(HTTPException) as expired:
        verify_telegram_login(replay, now=now)
    assert expired.value.status_code == 401

    future = _telegram_login_data(token, "12345678", now + timedelta(minutes=2))
    with pytest.raises(HTTPException) as invalid_future:
        verify_telegram_login(future, now=now)
    assert invalid_future.value.status_code == 401


async def _partner(db_session, telegram_id: str, project: str, amount: str) -> Partner:
    from decimal import Decimal

    partner = Partner(
        telegram_id=telegram_id,
        company_name="ООО Тест",
        project_name=project,
        status="active",
        balance_rub=Decimal(amount),
    )
    db_session.add(partner)
    await db_session.flush()
    return partner


async def test_cabinet_requires_signed_telegram_login_and_revokes_on_logout(
    client, db_session, monkeypatch,
):
    token = "bot:test-secret-only"
    monkeypatch.setattr(get_settings(), "telegram_bot_token", token)
    monkeypatch.setattr(get_settings(), "telegram_bot_username", "apineurobrobot")
    partner = await _partner(db_session, "12345678", "Тестовый проект", "1342.50")
    await db_session.commit()

    public = await client.get("/cabinet/overview")
    assert public.status_code == 303
    assert public.headers["location"] == "/cabinet/login"
    login = await client.get("/cabinet/login")
    assert login.status_code == 200
    assert "data-telegram-login" in login.text
    assert "HttpOnly" in login.headers["set-cookie"]
    assert (await client.post("/cabinet/auth/telegram", json={})).status_code == 401

    payload = _telegram_login_data(token, partner.telegram_id, datetime.now(UTC))
    auth = await client.post("/cabinet/auth/telegram", json=payload, headers={"origin": "http://testserver"})
    assert auth.status_code == 200
    assert auth.json() == {"ok": True}
    assert "HttpOnly" in auth.headers["set-cookie"]
    assert "SameSite=strict" in auth.headers["set-cookie"]

    overview = await client.get("/cabinet/overview")
    assert overview.status_code == 200
    assert "Тестовый проект" in overview.text
    assert "1 342.50 ₽" in overview.text
    assert "no-store" in overview.headers["cache-control"]

    for section in ("generations", "transactions", "payments", "keys", "models", "integration", "settings"):
        response = await client.get("/cabinet/" + section)
        assert response.status_code == 200, section
        assert "Тестовый проект" in response.text

    wrong = await client.get("/cabinet/unknown")
    assert wrong.status_code == 404

    logout = await client.post("/cabinet/logout", headers={"origin": "http://testserver"})
    assert logout.status_code == 303
    assert logout.headers["location"] == "/cabinet/login"
    revoked = await client.get("/cabinet/overview")
    assert revoked.status_code == 303
    assert revoked.headers["location"] == "/cabinet/login"


async def test_cabinet_rejects_unknown_or_suspended_partner(client, db_session, monkeypatch):
    token = "bot:test-secret-only"
    monkeypatch.setattr(get_settings(), "telegram_bot_token", token)
    partner = await _partner(db_session, "88881111", "Заблокированный", "80.00")
    partner.status = "suspended"
    await db_session.commit()

    await client.get("/cabinet/login")
    payload = _telegram_login_data(token, partner.telegram_id, datetime.now(UTC))
    response = await client.post("/cabinet/auth/telegram", json=payload)
    assert response.status_code == 403

    unknown = _telegram_login_data(token, "99991234", datetime.now(UTC))
    response = await client.post("/cabinet/auth/telegram", json=unknown)
    assert response.status_code == 403


async def test_cabinet_does_not_leak_other_partner_data_or_key_secrets(
    client, db_session, monkeypatch,
):
    token = "bot:test-secret-only"
    monkeypatch.setattr(get_settings(), "telegram_bot_token", token)
    owner = await _partner(db_session, "33335555", "Своя команда", "700.00")
    stranger = await _partner(db_session, "33336666", "Чужой секретный проект", "9999999.00")
    secret = "nrn_never_show_secret_key"
    db_session.add(ApiKey(
        partner_id=stranger.id, name="Секрет чужого проекта",
        key_hash=hash_secret(secret), key_prefix="nrn_test", is_active=True,
    ))
    db_session.add(ApiKey(
        partner_id=owner.id, name="Рабочий ключ",
        key_hash=hash_secret("nrn_owner_secret"), key_prefix="nrn_owne", is_active=True,
    ))
    await db_session.commit()
    await client.get("/cabinet/login")
    payload = _telegram_login_data(token, owner.telegram_id, datetime.now(UTC))
    assert (await client.post("/cabinet/auth/telegram", json=payload)).status_code == 200

    for section in ("overview", "keys", "settings"):
        response = await client.get("/cabinet/" + section)
        assert response.status_code == 200
        assert "Чужой секретный проект" not in response.text
        assert "9 999 999.00" not in response.text
        assert "Секрет чужого проекта" not in response.text
        assert secret not in response.text
        assert "nrn_owner_secret" not in response.text
    own_keys = await client.get("/cabinet/keys")
    assert "Рабочий ключ" in own_keys.text
    assert "nrn_owne" in own_keys.text


async def test_web_login_rejects_cross_origin_and_invalid_payload(client, db_session, monkeypatch):
    token = "bot:test-secret-only"
    monkeypatch.setattr(get_settings(), "telegram_bot_token", token)
    await _partner(db_session, "44441111", "Тест", "0.00")
    await db_session.commit()
    await client.get("/cabinet/login")
    payload = _telegram_login_data(token, "44441111", datetime.now(UTC))
    bad_origin = await client.post(
        "/cabinet/auth/telegram", json=payload, headers={"origin": "https://attacker.example"}
    )
    assert bad_origin.status_code == 403
    bad_type = await client.post(
        "/cabinet/auth/telegram", content="not-json", headers={"content-type": "text/plain"}
    )
    assert bad_type.status_code == 415
    too_big = await client.post(
        "/cabinet/auth/telegram", content="{" + "a" * 5000 + "}", headers={"content-type": "application/json"}
    )
    assert too_big.status_code == 413


@pytest.mark.parametrize("asset", ["security.py", ".env", "%2e%2e%2froutes.py"])
async def test_cabinet_static_allowlist_rejects_source_files(client, asset):
    assert (await client.get("/cabinet/assets/" + asset)).status_code == 404


@pytest.mark.parametrize("asset,mime", [("cabinet.css", "text/css"), ("cabinet-login.js", "text/javascript")])
async def test_cabinet_static_assets(client, asset, mime):
    response = await client.get("/cabinet/assets/" + asset)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(mime)
