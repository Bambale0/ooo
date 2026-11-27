"""Real PostgreSQL signup races and Redis proof expiry; no external provider calls."""

import asyncio
import os
import secrets
from uuid import uuid4

import pytest
from fastapi import Request
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.accounts.models import ConsentAcceptance, Partner, PartnerApplication, PartnerIdentity
from app.dashboard import web, yandex_signup
from app.infrastructure.config import get_settings
from app.infrastructure.database import Base
from app.providers.key_pool import add_pool_keys
from app.providers.models import ProviderCredential, ProviderKeyPoolItem


@pytest.mark.integration
async def test_signup_proof_real_redis_is_one_time_and_expires():
    if not os.getenv("TEST_POSTGRES_DATABASE_URL"):
        pytest.skip("CI infrastructure is not configured")
    import redis.asyncio as redis

    token = secrets.token_urlsafe(32)
    data = {"subject": "123", "csrf": "proof", "legal_version": "test"}
    await yandex_signup._save(token, data)
    assert await yandex_signup._read("bad-cookie") is None
    assert await yandex_signup._read(token) == data
    results = await asyncio.gather(*(yandex_signup._read(token, consume=True) for _ in range(6)))
    assert sum(result == data for result in results) == 1
    client = redis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        await yandex_signup._save(token, data)
        key = yandex_signup._key(token)
        assert 0 < await client.ttl(key) <= 600
        await client.expire(key, 0)
        assert await yandex_signup._read(token) is None
    finally:
        await client.aclose()


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["same_subject", "different_subjects", "same_proof"])
@pytest.mark.parametrize("shared", [False, True])
async def test_concurrent_yandex_signup_preserves_account_and_key_ownership(monkeypatch, kind, shared):
    url = os.getenv("TEST_POSTGRES_DATABASE_URL")
    if not url:
        pytest.skip("CI PostgreSQL is not configured")
    schema = "yandex_signup_" + uuid4().hex
    control = create_async_engine(url)
    async with control.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, pool_size=4, connect_args={"server_settings": {"search_path": schema}})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = get_settings()
    monkeypatch.setattr(settings, "public_api_base_url", "https://testserver")
    monkeypatch.setattr(settings, "yandex_client_id", "test-client")
    monkeypatch.setattr(settings, "yandex_client_secret", SecretStr("test-client-secret"))
    monkeypatch.setattr(settings, "yandex_callback_url", "https://testserver/cabinet/yandex/callback")
    monkeypatch.setattr(settings, "terms_url", "https://testserver/terms")
    monkeypatch.setattr(settings, "privacy_policy_url", "https://testserver/privacy")
    monkeypatch.setattr(settings, "argolink_registration_shared_pool_item_id", None)
    monkeypatch.setattr(settings, "argolink_registration_key_pool_json", None)
    monkeypatch.setattr(settings, "argolink_registration_claim_budget", 10)

    async def valid(self, api_key):
        return True

    monkeypatch.setattr("app.providers.argolink.ArgoLinkAdapter.validate_key", valid)
    tokens = [secrets.token_urlsafe(32) for _ in range(2)]
    if kind == "same_proof":
        tokens[1] = tokens[0]
    proofs = {tokens[i]: {
        "subject": str(123 + i) if kind == "different_subjects" else "123",
        "csrf": "csrf", "legal_version": settings.legal_document_version,
    } for i in range(2)}

    async def read(token, *, consume=False):
        return proofs.pop(token, None) if consume else proofs.get(token)

    issued = []

    async def store(token, partner_id):
        issued.append(partner_id)

    monkeypatch.setattr(yandex_signup, "_read", read)
    monkeypatch.setattr(web, "_store_session", store)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as setup:
            await add_pool_keys(
                setup, ["synthetic-parallel-one", "synthetic-parallel-two"], source="test", actor="test",
            )
            if shared:
                item = await setup.scalar(select(ProviderKeyPoolItem).limit(1))
                monkeypatch.setattr(settings, "argolink_registration_shared_pool_item_id", item.id)
            await setup.commit()

        async def signup(index):
            request = Request({
                "type": "http", "method": "POST", "path": "/cabinet/yandex/register",
                "headers": [(b"origin", b"https://testserver"),
                            (b"cookie", f"{yandex_signup._COOKIE}={tokens[index]}".encode())],
            })
            async with factory() as db:
                response = await yandex_signup.register(
                    request, db, csrf="csrf", company_name="Independent", project_name="Parallel",
                    accepted_terms="yes", accepted_privacy="yes",
                )
                return response.status_code

        results = await asyncio.wait_for(asyncio.gather(signup(0), signup(1)), timeout=30)
        assert sorted(results) == ([200, 403] if kind == "same_proof" else [200, 200])
        owners = 2 if kind == "different_subjects" else 1
        assert len(set(issued)) == owners
        async with factory() as check:
            for model in (Partner, PartnerApplication, PartnerIdentity):
                assert await check.scalar(select(func.count()).select_from(model)) == owners
            assert await check.scalar(select(func.count()).select_from(ConsentAcceptance)) == owners * 2
            assert await check.scalar(select(func.count()).select_from(ProviderCredential)) == (1 if shared else owners)
            partners = list(await check.scalars(select(Partner)))
            assert all(p.telegram_id is None and p.balance_rub == 0 and not p.is_admin for p in partners)
    finally:
        await engine.dispose()
        async with control.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await control.dispose()
