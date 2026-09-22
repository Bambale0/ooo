from app.infrastructure.config import get_settings
from app.infrastructure.security import decrypt_secret
from app.providers.models import ProviderCredential


class CredentialProbeAdapter:
    provider_name = "argolink"

    async def validate_key(self, api_key: str) -> bool:
        return api_key == "partner-secret-key"

    async def health_check(self) -> bool:
        return True


def probe_adapter(provider: str, *, api_key: str | None = None) -> CredentialProbeAdapter:
    assert provider == "argolink"
    return CredentialProbeAdapter()


async def test_provider_credential_is_scoped_to_application_and_encrypted(
    client,
    db_session,
    admin_headers,
    monkeypatch,
):
    monkeypatch.setattr("app.providers.router.get_provider_adapter", probe_adapter)

    first = await client.post(
        "/api/v1/accounts/applications",
        json={
            "telegram_id": "cred-app-1",
            "company_name": "First Partner",
            "project_name": "First Project",
            "accepted_terms": True,
            "accepted_privacy_policy": True,
        },
    )
    second = await client.post(
        "/api/v1/accounts/applications",
        json={
            "telegram_id": "cred-app-2",
            "company_name": "Second Partner",
            "project_name": "Second Project",
            "accepted_terms": True,
            "accepted_privacy_policy": True,
        },
    )
    assert first.status_code == 201
    assert second.status_code == 201

    credential_response = await client.post(
        "/api/v1/providers/credentials",
        headers=admin_headers,
        json={
            "provider": "argolink",
            "label": "first partner",
            "api_key": "partner-secret-key",
            "partner_application_id": first.json()["id"],
        },
    )
    assert credential_response.status_code == 201
    body = credential_response.json()
    assert body["partner_application_id"] == first.json()["id"]
    assert body["partner_id"] is None
    assert "api_key" not in body
    assert "encrypted_api_key" not in body

    credential = await db_session.get(ProviderCredential, body["id"])
    assert credential is not None
    assert credential.encrypted_api_key is not None
    assert credential.encrypted_api_key != "partner-secret-key"
    master_key = get_settings().provider_credentials_master_key
    assert master_key is not None
    assert decrypt_secret(credential.encrypted_api_key, master_key) == "partner-secret-key"

    blocked_second = await client.post(
        f"/api/v1/accounts/applications/{second.json()['id']}/approve",
        headers=admin_headers,
    )
    assert blocked_second.status_code == 409
    assert blocked_second.json()["detail"] == "required_provider_key_missing"

    approved_first = await client.post(
        f"/api/v1/accounts/applications/{first.json()['id']}/approve",
        headers=admin_headers,
    )
    assert approved_first.status_code == 200

    await db_session.refresh(credential)
    assert credential.partner_id == approved_first.json()["id"]
    assert credential.partner_application_id == first.json()["id"]


async def test_existing_job_uses_original_credential_after_rotation(db_session, monkeypatch):
    from app.accounts.models import Partner
    from app.infrastructure.security import encrypt_secret, hash_secret
    from app.providers.service import get_partner_provider_adapter

    partner = Partner(telegram_id="rotation-partner", company_name="Test", project_name="Test")
    db_session.add(partner)
    await db_session.flush()
    master_key = get_settings().provider_credentials_master_key
    old = ProviderCredential(
        partner_id=partner.id,
        provider="argolink",
        label="old",
        is_active=False,
        key_hash=hash_secret("old-key"),
        key_prefix="old",
        encrypted_api_key=encrypt_secret("old-key", master_key),
    )
    new = ProviderCredential(
        partner_id=partner.id,
        provider="argolink",
        label="new",
        is_active=True,
        key_hash=hash_secret("new-key"),
        key_prefix="new",
        encrypted_api_key=encrypt_secret("new-key", master_key),
    )
    db_session.add_all([old, new])
    await db_session.flush()
    monkeypatch.setattr("app.providers.service.get_provider_adapter", lambda provider, api_key: api_key)
    assert await get_partner_provider_adapter(db_session, partner.id, "argolink", credential_id=old.id) == "old-key"
    assert await get_partner_provider_adapter(db_session, partner.id, "argolink") == "new-key"
