"""Shared upstream credentials must not collapse partner billing or API identities."""

from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.accounts.models import ApiKey, Partner, PartnerApplication
from app.accounts.router import auto_activate_application
from app.infrastructure.config import get_settings
from app.infrastructure.security import encrypt_secret, hash_secret
from app.providers.key_pool import add_pool_keys
from app.providers.models import ProviderCredential, ProviderKeyPoolItem
from app.providers.service import (
    get_active_provider_credential,
    get_partner_provider_adapter,
    has_provider_runtime_credential,
)
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
