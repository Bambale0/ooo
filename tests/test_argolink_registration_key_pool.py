import asyncio
import json
import os
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.accounts.models import ApiKey, Partner, PartnerApplication
from app.accounts.router import auto_activate_application, delete_partner, transfer_telegram
from app.accounts.schemas import DeletePartnerCreate, TransferTelegramCreate
from app.infrastructure.config import get_settings
from app.infrastructure.database import Base
from app.infrastructure.security import decrypt_secret, hash_secret
from app.providers.key_pool import add_pool_keys, parse_key_list, pool_counts, provision_application_key
from app.providers.models import ProviderCredential, ProviderKeyPoolItem
from app.providers.router import create_provider_credential
from app.providers.schemas import ProviderCredentialCreate
from app.providers.service import has_provider_runtime_credential
from app.referrals import models as referral_models  # noqa: F401
from app.telegram.handlers import activate_application_and_issue_key
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


async def test_handled_decrypt_failure_does_not_consume_pool_key(db_session, monkeypatch):
    monkeypatch.setattr("app.providers.argolink.ArgoLinkAdapter.validate_key", _valid)
    monkeypatch.setattr(get_settings(), "admin_telegram_id", "999")
    await add_pool_keys(
        db_session,
        ["sk-pool-decrypt-failure-00000000000001"],
        source="test",
        actor="test",
    )
    application = PartnerApplication(telegram_id="decrypt-failure", company_name="C", project_name="P")
    db_session.add(application)
    await db_session.commit()

    def invalid_ciphertext(*args, **kwargs):
        raise ValueError("synthetic decrypt failure")

    monkeypatch.setattr("app.providers.key_pool.decrypt_secret", invalid_ciphertext)
    assert await activate_application_and_issue_key(db_session, application) is None
    await db_session.commit()

    await db_session.refresh(application)
    item = await db_session.scalar(select(ProviderKeyPoolItem))
    assert application.status == "pending"
    assert item.status == "available" and item.encrypted_api_key
    assert await db_session.scalar(select(func.count()).select_from(ProviderCredential)) == 0
    assert await db_session.scalar(select(func.count()).select_from(BotNotification)) == 1


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


def test_bootstrap_secret_requires_a_json_array():
    with pytest.raises(HTTPException) as error:
        parse_key_list('{"key":"not-an-array"}')
    assert error.value.detail == "provider_key_pool_configuration_invalid"


async def test_manual_credential_cannot_reuse_a_pooled_key(db_session, monkeypatch):
    monkeypatch.setattr("app.providers.argolink.ArgoLinkAdapter.validate_key", _valid)
    key = "sk-pool-cross-table-duplicate-000000001"
    await add_pool_keys(db_session, [key], source="test", actor="test")
    application = PartnerApplication(telegram_id="cross-table", company_name="C", project_name="P")
    db_session.add(application)
    await db_session.flush()

    with pytest.raises(HTTPException) as error:
        await create_provider_credential(
            ProviderCredentialCreate(label="manual", api_key=key, partner_application_id=application.id),
            db_session,
        )
    assert error.value.detail == "provider_key_pool_duplicate"
    assert await db_session.scalar(select(func.count()).select_from(ProviderCredential)) == 0


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


async def test_delete_and_reregister_reuses_upstream_key_without_consuming_second(db_session, monkeypatch):
    monkeypatch.setattr("app.providers.argolink.ArgoLinkAdapter.validate_key", _valid)
    keys = ["sk-pool-reregister-a-000000000000001", "sk-pool-reregister-b-000000000000002"]
    await add_pool_keys(db_session, keys, source="test", actor="test")
    first_application = PartnerApplication(
        telegram_id="same-telegram", company_name="First", project_name="First"
    )
    db_session.add(first_application)
    await db_session.flush()
    first_partner = await auto_activate_application(first_application.id, db_session)
    first_credential = await db_session.scalar(
        select(ProviderCredential).where(ProviderCredential.partner_id == first_partner.id)
    )
    first_hash = first_credential.key_hash
    await delete_partner(first_partner.id, DeletePartnerCreate(reason="test re-registration"), db_session)

    second_application = PartnerApplication(
        telegram_id="same-telegram", company_name="Second", project_name="Second"
    )
    db_session.add(second_application)
    await db_session.flush()
    second_partner = await auto_activate_application(second_application.id, db_session)
    await db_session.commit()

    credentials = list(await db_session.scalars(select(ProviderCredential)))
    available, assigned = await pool_counts(db_session)
    assert len(credentials) == 1
    assert credentials[0].key_hash == first_hash
    assert credentials[0].partner_id == second_partner.id
    assert credentials[0].partner_application_id == second_application.id
    assert (available, assigned) == (1, 1)


async def test_transferred_identity_reuses_key_and_former_identity_cannot_inherit_it(db_session, monkeypatch):
    monkeypatch.setattr("app.providers.argolink.ArgoLinkAdapter.validate_key", _valid)
    keys = [f"sk-pool-transfer-{index}-000000000000001" for index in range(3)]
    await add_pool_keys(db_session, keys, source="test", actor="test")
    original_application = PartnerApplication(
        telegram_id="111111", company_name="Original", project_name="Original"
    )
    db_session.add(original_application)
    await db_session.flush()
    original_partner = await auto_activate_application(original_application.id, db_session)
    original_credential = await db_session.scalar(
        select(ProviderCredential).where(ProviderCredential.partner_id == original_partner.id)
    )
    original_hash = original_credential.key_hash

    await transfer_telegram(
        original_partner.id,
        TransferTelegramCreate(telegram_id="222222", reason="verified transfer"),
        db_session,
    )
    assert original_credential.registration_telegram_id == "222222"
    await delete_partner(original_partner.id, DeletePartnerCreate(reason="test transferred deletion"), db_session)

    transferred_application = PartnerApplication(
        telegram_id="222222", company_name="Transferred", project_name="Transferred"
    )
    former_application = PartnerApplication(
        telegram_id="111111", company_name="Former", project_name="Former"
    )
    db_session.add_all([transferred_application, former_application])
    await db_session.flush()
    transferred_partner = await auto_activate_application(transferred_application.id, db_session)
    former_partner = await auto_activate_application(former_application.id, db_session)
    await db_session.commit()

    transferred_credential = await db_session.scalar(
        select(ProviderCredential).where(ProviderCredential.partner_id == transferred_partner.id)
    )
    former_credential = await db_session.scalar(
        select(ProviderCredential).where(ProviderCredential.partner_id == former_partner.id)
    )
    available, assigned = await pool_counts(db_session)
    assert transferred_credential.key_hash == original_hash
    assert transferred_credential.registration_telegram_id == "222222"
    assert former_credential.key_hash != original_hash
    assert former_credential.registration_telegram_id == "111111"
    assert (available, assigned) == (1, 2)


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
    assert action.payload == {"count": 2, "added": 2}
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
        keys = [f"sk-pool-race-{i}-000000000000000001" for i in range(4)]
        await add_pool_keys(setup, keys, source="test", actor="test")
        applications = [
            PartnerApplication(telegram_id=f"race-{i}", company_name="C", project_name="P") for i in range(4)
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
    assert len(set(hashes)) == 4
    async with factory() as check:
        assert await check.scalar(
            select(func.count()).select_from(ProviderKeyPoolItem).where(ProviderKeyPoolItem.status == "assigned")
        ) == 4
    await engine.dispose()


@pytest.mark.integration
async def test_postgres_four_way_claim_and_cross_table_insert_race(monkeypatch):
    url = os.getenv("TEST_POSTGRES_DATABASE_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_DATABASE_URL is not configured")
    monkeypatch.setattr("app.providers.argolink.ArgoLinkAdapter.validate_key", _valid)
    engine = create_async_engine(url, pool_size=8, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid4().hex
    claim_keys = [f"sk-pool-pg-{suffix}-{index}" for index in range(4)]
    race_key = f"sk-pool-pg-race-{suffix}"
    application_ids: list[str] = []
    try:
        async with factory() as setup:
            await add_pool_keys(setup, claim_keys, source="test", actor="test")
            applications = [
                PartnerApplication(
                    telegram_id=f"pool-pg-{suffix}-{index}", company_name="C", project_name="P"
                )
                for index in range(5)
            ]
            setup.add_all(applications)
            await setup.commit()
            application_ids = [application.id for application in applications]

        async def claim(application_id: str) -> str:
            async with factory() as session:
                credential = await provision_application_key(session, application_id)
                await session.commit()
                return credential.key_hash

        hashes = await asyncio.wait_for(
            asyncio.gather(*(claim(application_id) for application_id in application_ids[:4])),
            timeout=20,
        )
        assert len(set(hashes)) == 4

        async def add_racing_pool_key() -> str:
            async with factory() as session:
                try:
                    await add_pool_keys(session, [race_key], source="test", actor="test")
                    await session.commit()
                    return "pool"
                except HTTPException as exc:
                    await session.rollback()
                    return str(exc.detail)

        async def add_racing_credential() -> str:
            async with factory() as session:
                try:
                    await create_provider_credential(
                        ProviderCredentialCreate(
                            label="race",
                            api_key=race_key,
                            partner_application_id=application_ids[4],
                        ),
                        session,
                    )
                    await session.commit()
                    return "credential"
                except HTTPException as exc:
                    await session.rollback()
                    return str(exc.detail)

        race_results = await asyncio.wait_for(
            asyncio.gather(add_racing_pool_key(), add_racing_credential()),
            timeout=20,
        )
        assert "provider_key_pool_duplicate" in race_results
        assert len({result for result in race_results if result in {"pool", "credential"}}) == 1
        async with factory() as check:
            race_hash = hash_secret(race_key)
            pool_count = await check.scalar(
                select(func.count()).select_from(ProviderKeyPoolItem).where(ProviderKeyPoolItem.key_hash == race_hash)
            )
            credential_count = await check.scalar(
                select(func.count()).select_from(ProviderCredential).where(ProviderCredential.key_hash == race_hash)
            )
            assert pool_count + credential_count == 1
    finally:
        async with factory() as cleanup:
            all_hashes = [hash_secret(key) for key in [*claim_keys, race_key]]
            await cleanup.execute(delete(ProviderCredential).where(ProviderCredential.key_hash.in_(all_hashes)))
            await cleanup.execute(delete(ProviderKeyPoolItem).where(ProviderKeyPoolItem.key_hash.in_(all_hashes)))
            if application_ids:
                await cleanup.execute(
                    delete(PartnerApplication).where(PartnerApplication.id.in_(application_ids))
                )
            await cleanup.commit()
        await engine.dispose()
