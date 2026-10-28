import asyncio
import json

import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.accounts.models import ApiKey, Partner, PartnerApplication
from app.accounts.router import auto_activate_application
from app.infrastructure.config import get_settings
from app.infrastructure.database import Base
from app.infrastructure.security import decrypt_secret
from app.providers.key_pool import add_pool_keys, provision_application_key
from app.providers.models import ProviderCredential, ProviderKeyPoolItem
from app.providers.service import has_provider_runtime_credential
from app.referrals import models as referral_models  # noqa: F401
from app.telegram.models import BotAction, BotNotification
from tests.test_telegram_cabinet import cabinet as cabinet


async def _valid(self, api_key):
    return True


async def test_two_registrations_receive_two_distinct_upstream_keys(db_session, monkeypatch):
    keys = ["sk-pool-test-a-000000000000000001", "sk-pool-test-b-000000000000000002"]
    monkeypatch.setattr(get_settings(), "argolink_registration_key_pool_json", SecretStr(json.dumps(keys)))
    monkeypatch.setattr("app.providers.argolink.ArgoLinkAdapter.validate_key", _valid)
    applications = [
        PartnerApplication(telegram_id=str(index), company_name=f"C{index}", project_name=f"P{index}")
        for index in (1, 2)
    ]
    db_session.add_all(applications)
    await db_session.flush()

    partners = [await auto_activate_application(application.id, db_session) for application in applications]
    await db_session.commit()

    credentials = list(await db_session.scalars(select(ProviderCredential).order_by(ProviderCredential.key_hash)))
    assert len(credentials) == 2
    assert {credential.key_hash for credential in credentials} == {
        item.key_hash for item in await db_session.scalars(select(ProviderKeyPoolItem))
    }
    assert all(partner.requires_dedicated_argolink_credential for partner in partners)
    plaintext = {
        decrypt_secret(credential.encrypted_api_key, get_settings().provider_credentials_master_key)
        for credential in credentials
    }
    assert plaintext == set(keys)
    assert all(item.encrypted_api_key is None and item.status == "assigned" for item in await db_session.scalars(
        select(ProviderKeyPoolItem)
    ))


async def test_claim_rolls_back_to_available_with_secret_intact(db_session, monkeypatch):
    monkeypatch.setattr("app.providers.argolink.ArgoLinkAdapter.validate_key", _valid)
    await add_pool_keys(
        db_session,
        ["sk-pool-rollback-000000000000000001"],
        source="test",
        actor="test",
    )
    application = PartnerApplication(telegram_id="rollback", company_name="C", project_name="P")
    db_session.add(application)
    await db_session.commit()

    await provision_application_key(db_session, application.id)
    await db_session.rollback()

    item = await db_session.scalar(select(ProviderKeyPoolItem))
    assert item.status == "available" and item.encrypted_api_key
    assert await db_session.scalar(select(func.count()).select_from(ProviderCredential)) == 0


async def test_duplicate_batch_is_rejected_without_partial_rows(db_session, monkeypatch):
    monkeypatch.setattr("app.providers.argolink.ArgoLinkAdapter.validate_key", _valid)
    duplicate = "sk-pool-duplicate-00000000000000001"
    with pytest.raises(HTTPException) as error:
        await add_pool_keys(
            db_session,
            [duplicate, duplicate],
            source="test",
            actor="test",
        )
    assert error.value.detail == "provider_key_pool_duplicate"
    assert await db_session.scalar(select(func.count()).select_from(ProviderKeyPoolItem)) == 0


async def test_dedicated_partner_never_falls_back_to_shared_key(db_session, monkeypatch):
    monkeypatch.setattr("app.providers.argolink.ArgoLinkAdapter.validate_key", _valid)
    monkeypatch.setattr(get_settings(), "argolink_api_key", "shared-must-not-be-used")
    await add_pool_keys(
        db_session,
        ["sk-pool-dedicated-000000000000000001"],
        source="test",
        actor="test",
    )
    application = PartnerApplication(telegram_id="dedicated", company_name="C", project_name="P")
    db_session.add(application)
    await db_session.flush()
    partner = await auto_activate_application(application.id, db_session)
    credential = await db_session.scalar(select(ProviderCredential).where(ProviderCredential.partner_id == partner.id))
    credential.is_active = False
    await db_session.flush()

    assert not await has_provider_runtime_credential(db_session, partner.id, "argolink")


async def test_empty_pool_keeps_application_pending_and_retry_uses_same_application(
    cabinet, db_session, monkeypatch
):
    feed, _ = cabinet
    monkeypatch.setattr(get_settings(), "argolink_registration_key_pool_json", None)
    monkeypatch.setattr(get_settings(), "terms_url", "https://example.org/terms")
    monkeypatch.setattr(get_settings(), "privacy_policy_url", "https://example.org/privacy")
    await feed(callback="register")
    await feed(callback="accept_legal")
    await feed(text="Компания")
    screen = (await feed(text="Проект"))[-1]

    application = await db_session.scalar(select(PartnerApplication))
    assert application.status == "pending"
    assert "ожидает отдельный ключ ArgoLink" in screen.text
    assert await db_session.scalar(select(func.count()).select_from(Partner)) == 0
    assert await db_session.scalar(select(func.count()).select_from(ApiKey)) == 0
    assert await db_session.scalar(select(func.count()).select_from(BotNotification)) == 1

    await add_pool_keys(
        db_session,
        ["sk-pool-retry-000000000000000001"],
        source="test",
        actor="test",
    )
    await db_session.commit()
    screen = (await feed(text="/start"))[-1]
    await db_session.refresh(application)
    assert application.status == "approved"
    assert "nrn_" in screen.text
    assert await db_session.scalar(select(func.count()).select_from(PartnerApplication)) == 1
    assert await db_session.scalar(select(func.count()).select_from(ProviderCredential)) == 1


async def test_admin_pool_add_is_authorized_encrypted_and_all_or_nothing(cabinet, db_session, monkeypatch):
    feed, _ = cabinet
    monkeypatch.setattr(get_settings(), "argolink_registration_key_pool_json", None)

    await feed(user=123, callback="admin_argolink_pool_add")
    assert await db_session.scalar(select(func.count()).select_from(BotAction)) == 0

    calls = 0

    async def second_invalid(self, api_key):
        nonlocal calls
        calls += 1
        return calls != 2

    monkeypatch.setattr("app.providers.argolink.ArgoLinkAdapter.validate_key", second_invalid)
    await feed(user=999, callback="admin_argolink_pool_add")
    await feed(
        user=999,
        text="sk-pool-admin-valid-00000000000000001\nsk-pool-admin-invalid-000000000000002",
    )
    assert await db_session.scalar(select(func.count()).select_from(ProviderKeyPoolItem)) == 0
    assert await db_session.scalar(select(func.count()).select_from(BotAction)) == 0

    monkeypatch.setattr("app.providers.argolink.ArgoLinkAdapter.validate_key", _valid)
    await feed(user=999, callback="admin_argolink_pool_add")
    secret_text = "sk-pool-admin-a-000000000000000001\nsk-pool-admin-b-000000000000000002"
    screen = (await feed(user=999, text=secret_text))[-1]
    action = await db_session.scalar(select(BotAction))
    assert "sk-pool" not in json.dumps(action.payload)
    assert "sk-pool" not in screen.text
    await feed(user=999, callback=f"confirm:{action.id}")
    assert action.status == "applied"
    assert await db_session.scalar(select(func.count()).select_from(ProviderKeyPoolItem)) == 2
    pool_screen = (await feed(user=999, callback="admin_argolink_pool"))[-1]
    assert "Свободно: 2" in pool_screen.text and "sk-" not in pool_screen.text


async def test_concurrent_claims_do_not_assign_the_same_key(tmp_path, monkeypatch):
    monkeypatch.setattr("app.providers.argolink.ArgoLinkAdapter.validate_key", _valid)
    database = tmp_path / "pool-concurrency.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}", connect_args={"timeout": 30})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as setup:
        await add_pool_keys(
            setup,
            ["sk-pool-race-a-000000000000000001", "sk-pool-race-b-000000000000000002"],
            source="test",
            actor="test",
        )
        applications = [
            PartnerApplication(telegram_id=f"race-{i}", company_name="C", project_name="P") for i in (1, 2)
        ]
        setup.add_all(applications)
        await setup.commit()
        ids = [application.id for application in applications]

    async def claim(application_id):
        async with factory() as session:
            credential = await provision_application_key(session, application_id)
            await session.commit()
            return credential.key_hash

    hashes = await asyncio.gather(*(claim(application_id) for application_id in ids))
    assert len(set(hashes)) == 2
    async with factory() as check:
        assert await check.scalar(
            select(func.count()).select_from(ProviderKeyPoolItem).where(ProviderKeyPoolItem.status == "assigned")
        ) == 2
    await engine.dispose()
