"""Shared upstream credentials must not collapse partner billing or API identities."""

import asyncio
import os
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.accounts.models import ApiKey, Partner, PartnerApplication
from app.accounts.router import auto_activate_application, delete_partner
from app.accounts.schemas import DeletePartnerCreate
from app.infrastructure.config import get_settings
from app.infrastructure.database import Base
from app.infrastructure.security import encrypt_secret, hash_secret
from app.providers.key_pool import add_pool_keys
from app.providers.models import PartnerProviderCredentialBinding, ProviderCredential, ProviderKeyPoolItem
from app.providers.service import (
    get_active_provider_credential,
    get_partner_provider_adapter,
    has_provider_runtime_credential,
)
from app.referrals import models as referral_models  # noqa: F401
from app.telegram.handlers import activate_application_and_issue_key
from tests.test_telegram_cabinet import cabinet as cabinet


async def shared_pool(db, monkeypatch):
    async def valid(self, api_key):
        return True

    monkeypatch.setattr("app.providers.argolink.ArgoLinkAdapter.validate_key", valid)
    monkeypatch.setattr(get_settings(), "argolink_registration_key_pool_json", None)
    await add_pool_keys(
        db,
        ["synthetic-first-registration-key", "synthetic-second-registration-key"],
        source="test",
        actor="test",
    )
    rows = list(await db.scalars(
        select(ProviderKeyPoolItem).order_by(ProviderKeyPoolItem.created_at, ProviderKeyPoolItem.id)
    ))
    # Older revisions ignore this opt-in and incorrectly consume the first row.
    monkeypatch.setitem(get_settings().__dict__, "argolink_registration_shared_pool_item_id", rows[1].id)
    await db.commit()
    return rows


async def application(db, user):
    row = PartnerApplication(telegram_id=user, company_name="Company", project_name="Project")
    db.add(row)
    await db.flush()
    return row


async def test_new_partner_uses_selected_second_pool_key(db_session, monkeypatch):
    first, second = await shared_pool(db_session, monkeypatch)
    signup = await application(db_session, "new-shared-partner")
    owner = await auto_activate_application(signup.id, db_session)
    credential = await get_active_provider_credential(db_session, owner.id, "argolink")

    assert credential.key_hash == second.key_hash
    assert credential.partner_id is None
    assert credential.partner_application_id is None
    assert first.status == "available" and first.encrypted_api_key
    assert second.status == "shared" and second.encrypted_api_key is None
    assert owner.balance_rub == Decimal("0")
    assert owner.requires_dedicated_argolink_credential  # no legacy runtime fallback


async def test_five_bot_signups_share_upstream_but_not_accounts_or_keys(cabinet, db_session, monkeypatch):
    feed, _ = cabinet
    first, second = await shared_pool(db_session, monkeypatch)
    monkeypatch.setattr(get_settings(), "terms_url", "https://example.org/terms")
    monkeypatch.setattr(get_settings(), "privacy_policy_url", "https://example.org/privacy")
    monkeypatch.setattr(get_settings(), "argolink_registration_claim_budget", 1)
    displayed = []
    for user in range(701, 706):
        await feed(user=user, text="/start")
        await feed(user=user, callback="accept_legal")
        await feed(user=user, text=f"Company {user}")
        screen = (await feed(user=user, text=f"Project {user}"))[-1]
        assert "nrn_" in screen.text
        assert "synthetic-" not in screen.text
        displayed.append(screen.text)
        await feed(user=user, text="/start")

    partners = list(await db_session.scalars(select(Partner)))
    assert len(partners) == 5
    assert len(set(displayed)) == 5
    assert await db_session.scalar(select(func.count()).select_from(ApiKey)) == 5
    assert await db_session.scalar(select(func.count()).select_from(PartnerApplication)) == 5
    assert await db_session.scalar(select(func.count()).select_from(ProviderCredential)) == 1
    assert all(partner.balance_rub == Decimal("0") for partner in partners)
    credentials = [await get_active_provider_credential(db_session, partner.id, "argolink") for partner in partners]
    assert {credential.key_hash for credential in credentials} == {second.key_hash}
    partners[0].balance_rub = Decimal("123.45")
    await db_session.flush()
    assert all(partner.balance_rub == Decimal("0") for partner in partners[1:])
    await db_session.refresh(first)
    assert first.status == "available"


async def test_shared_credential_unavailable_never_uses_legacy_fallback(db_session, monkeypatch):
    await shared_pool(db_session, monkeypatch)
    signup = await application(db_session, "shared-unavailable")
    owner = await auto_activate_application(signup.id, db_session)
    credential = await get_active_provider_credential(db_session, owner.id, "argolink")
    credential.is_active = False
    await db_session.flush()
    assert not await has_provider_runtime_credential(db_session, owner.id, "argolink")
    with pytest.raises(HTTPException) as error:
        await get_partner_provider_adapter(db_session, owner.id, "argolink")
    assert error.value.status_code == 503


async def test_unrelated_partner_cannot_use_shared_credential_id(db_session, monkeypatch):
    await shared_pool(db_session, monkeypatch)
    signup = await application(db_session, "shared-owner")
    owner = await auto_activate_application(signup.id, db_session)
    credential = await get_active_provider_credential(db_session, owner.id, "argolink")
    outsider = Partner(telegram_id="unrelated", company_name="Existing", project_name="Existing")
    db_session.add(outsider)
    await db_session.flush()
    with pytest.raises(HTTPException) as error:
        await get_partner_provider_adapter(db_session, outsider.id, "argolink", credential_id=credential.id)
    assert error.value.status_code == 503
    assert not outsider.requires_dedicated_argolink_credential
    assert await get_active_provider_credential(db_session, outsider.id, "argolink") is None


async def test_dedicated_override_does_not_break_other_shared_partners(db_session, monkeypatch):
    _, selected = await shared_pool(db_session, monkeypatch)
    owners = []
    for user in ("shared-a", "shared-b"):
        signup = await application(db_session, user)
        owners.append(await auto_activate_application(signup.id, db_session))
    old = await get_active_provider_credential(db_session, owners[0].id, "argolink")
    plaintext = "synthetic-later-dedicated-key"
    dedicated = ProviderCredential(
        provider="argolink", label="Dedicated", partner_id=owners[0].id,
        key_hash=hash_secret(plaintext), key_prefix=plaintext[:8],
        encrypted_api_key=encrypt_secret(plaintext, get_settings().provider_credentials_master_key),
    )
    db_session.add(dedicated)
    await db_session.flush()
    assert (await get_active_provider_credential(db_session, owners[0].id, "argolink")).id == dedicated.id
    assert (await get_active_provider_credential(db_session, owners[1].id, "argolink")).key_hash == selected.key_hash
    # Already submitted tasks remain pinned to the original upstream account.
    assert await get_partner_provider_adapter(db_session, owners[0].id, "argolink", credential_id=old.id)


async def test_bad_shared_configuration_keeps_signup_pending(db_session, monkeypatch):
    first, second = await shared_pool(db_session, monkeypatch)
    monkeypatch.setitem(get_settings().__dict__, "argolink_registration_shared_pool_item_id", "missing-pool-item")
    signup = await application(db_session, "invalid-config")
    assert await activate_application_and_issue_key(db_session, signup) is None
    assert signup.status == "pending"
    assert await db_session.scalar(select(func.count()).select_from(Partner)) == 0
    assert await db_session.scalar(select(func.count()).select_from(ApiKey)) == 0
    assert first.status == second.status == "available"


async def test_assigned_pool_key_is_never_stolen_for_sharing(db_session, monkeypatch):
    first, second = await shared_pool(db_session, monkeypatch)
    second.status = "assigned"
    await db_session.flush()
    signup = await application(db_session, "already-assigned")
    assert await activate_application_and_issue_key(db_session, signup) is None
    assert await db_session.scalar(select(func.count()).select_from(Partner)) == 0
    await db_session.refresh(first)
    await db_session.refresh(second)
    assert first.status == "available" and second.status == "assigned"


async def test_internal_key_failure_rolls_back_shared_promotion_and_binding(db_session, monkeypatch):
    first, second = await shared_pool(db_session, monkeypatch)
    signup = await application(db_session, "atomic-signup")

    async def fail(*args, **kwargs):
        raise HTTPException(503, "provider_key_pool_unavailable")

    monkeypatch.setattr("app.telegram.handlers.create_partner_api_key", fail)
    assert await activate_application_and_issue_key(db_session, signup) is None
    await db_session.commit()
    await db_session.refresh(signup)
    await db_session.refresh(second)
    assert signup.status == "pending"
    assert second.status == "available" and second.encrypted_api_key
    for model in (Partner, ApiKey, ProviderCredential, PartnerProviderCredentialBinding):
        assert await db_session.scalar(select(func.count()).select_from(model)) == 0


async def test_delete_and_reregister_does_not_revoke_other_tenants(db_session, monkeypatch):
    await shared_pool(db_session, monkeypatch)
    first_signup = await application(db_session, "shared-delete")
    first, first_key = await activate_application_and_issue_key(db_session, first_signup)
    second_signup = await application(db_session, "shared-survivor")
    second, _ = await activate_application_and_issue_key(db_session, second_signup)
    original = await get_active_provider_credential(db_session, first.id, "argolink")
    await delete_partner(first.id, DeletePartnerCreate(reason="Test deletion"), db_session)
    replacement_signup = await application(db_session, "shared-delete")
    replacement, _ = await activate_application_and_issue_key(db_session, replacement_signup)
    assert replacement.id != first.id
    assert replacement.balance_rub == Decimal("0")
    assert not (await db_session.get(ApiKey, first_key.id)).is_active
    assert (await get_active_provider_credential(db_session, replacement.id, "argolink")).id == original.id
    assert (await get_active_provider_credential(db_session, second.id, "argolink")).id == original.id
    assert original.is_active
    assert await db_session.scalar(select(func.count()).select_from(ProviderCredential)) == 1


async def test_inactive_binding_blocks_new_work_but_preserves_polling(db_session, monkeypatch):
    await shared_pool(db_session, monkeypatch)
    signup = await application(db_session, "shared-revoked-binding")
    owner = await auto_activate_application(signup.id, db_session)
    credential = await get_active_provider_credential(db_session, owner.id, "argolink")
    binding = await db_session.get(PartnerProviderCredentialBinding, (owner.id, credential.id))
    binding.is_active = False
    await db_session.flush()
    assert not await has_provider_runtime_credential(db_session, owner.id, "argolink")
    assert await get_partner_provider_adapter(db_session, owner.id, "argolink", credential_id=credential.id)


@pytest.mark.integration
async def test_postgres_parallel_shared_signups_create_one_credential(monkeypatch):
    url = os.getenv("TEST_POSTGRES_DATABASE_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_DATABASE_URL is not configured")
    schema = "shared_signup_" + uuid4().hex
    control = create_async_engine(url)
    async with control.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        url, pool_size=8, max_overflow=0,
        connect_args={"server_settings": {"search_path": schema}},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as setup:
            await shared_pool(setup, monkeypatch)
        monkeypatch.setattr(get_settings(), "argolink_registration_claim_budget", 1)

        async def signup(index):
            async with factory() as session:
                row = await application(session, f"parallel-shared-{index}")
                owner, key = await activate_application_and_issue_key(session, row)
                await session.commit()
                return owner.id, key.id

        registrations = await asyncio.wait_for(asyncio.gather(*(signup(i) for i in range(6))), timeout=25)
        assert len({owner_id for owner_id, _ in registrations}) == 6
        async with factory() as check:
            assert await check.scalar(select(func.count()).select_from(ProviderCredential)) == 1
            assert await check.scalar(select(func.count()).select_from(PartnerProviderCredentialBinding)) == 6
            assert await check.scalar(select(func.count()).select_from(ApiKey)) == 6
            assert await check.scalar(select(func.count()).select_from(ProviderKeyPoolItem).where(
                ProviderKeyPoolItem.status == "available"
            )) == 1
    finally:
        await engine.dispose()
        async with control.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await control.dispose()


async def test_shared_key_bills_only_its_caller_and_rejects_zero_balance(client, db_session, monkeypatch):
    import httpx

    from app.billing.models import LedgerEntry
    from app.providers.argolink import ArgoLinkAdapter
    from tests.test_native_inference import setup

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={
            "id": "synthetic-provider-response",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        })

    legacy, _, upstream = await setup(db_session, monkeypatch, handler)
    _, selected = await shared_pool(db_session, monkeypatch)
    selected_hash = selected.key_hash
    owners = []
    keys = []
    for user in ("shared-funded", "shared-empty"):
        signup = await application(db_session, user)
        owner, key = await activate_application_and_issue_key(db_session, signup)
        owners.append(owner)
        keys.append(key)
    owners[0].balance_rub = Decimal("10.00")
    owners[0].cost_coverage_rub = Decimal("10.00")
    await db_session.commit()
    routed_hashes = []

    def adapter(provider, *, api_key):
        assert provider == "argolink"
        routed_hashes.append(hash_secret(api_key))
        return ArgoLinkAdapter(api_key=api_key, client=upstream)

    monkeypatch.setattr("app.inference.router.get_partner_provider_adapter", get_partner_provider_adapter)
    monkeypatch.setattr("app.providers.service.get_provider_adapter", adapter)
    body = {"model": "gpt-5.4", "input": "Hello", "max_output_tokens": 128}
    headers = {"Authorization": f"Bearer {keys[0].api_key}", "Idempotency-Key": "shared-billing-test"}
    response = await client.post("/v1/responses", json=body, headers=headers)
    assert response.status_code == 200
    again = await client.post("/v1/responses", json=body, headers=headers)
    assert again.status_code == 200 and again.json() == response.json()
    empty = await client.post("/v1/responses", json=body, headers={
        "Authorization": f"Bearer {keys[1].api_key}", "Idempotency-Key": "shared-billing-test",
    })
    assert empty.status_code == 402
    assert len(calls) == 1
    assert routed_hashes and set(routed_hashes) == {selected_hash}
    await db_session.refresh(owners[0])
    await db_session.refresh(owners[1])
    await db_session.refresh(legacy)
    assert owners[0].balance_rub == Decimal("9.97")
    assert owners[1].balance_rub == Decimal("0")
    assert legacy.balance_rub == Decimal("1000000")
    entries = list(await db_session.scalars(select(LedgerEntry)))
    assert len(entries) == 1 and entries[0].partner_id == owners[0].id
    denied = await client.get(f"/api/v1/generations/{response.json()['id']}", headers={
        "Authorization": f"Bearer {keys[1].api_key}",
    })
    assert denied.status_code == 404
    await upstream.aclose()
