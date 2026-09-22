from app.infrastructure.config import get_settings
from app.infrastructure.security import decrypt_secret
from app.providers.models import ProviderCredential


async def test_provider_credential_is_encrypted_and_bound_to_application(
    client,
    db_session,
    admin_headers,
    monkeypatch,
):
    class ValidatingAdapter:
        async def validate_key(self, api_key: str) -> bool:
            return api_key == "partner-upstream-secret"

    def fake_adapter(provider: str, *, api_key: str | None = None):
        assert provider == "argolink"
        assert api_key == "partner-upstream-secret"
        return ValidatingAdapter()

    monkeypatch.setattr("app.providers.router.get_provider_adapter", fake_adapter)

    application = await client.post(
        "/api/v1/accounts/applications",
        json={
            "telegram_id": "provider-credential-test",
            "company_name": "Credential Partner",
            "project_name": "Credential Bot",
        },
    )
    assert application.status_code == 201
    application_id = application.json()["id"]

    response = await client.post(
        "/api/v1/providers/credentials",
        headers=admin_headers,
        json={
            "provider": "argolink",
            "label": "primary",
            "api_key": "partner-upstream-secret",
            "partner_application_id": application_id,
        },
    )
    assert response.status_code == 201
    credential_id = response.json()["id"]
    assert "api_key" not in response.json()

    credential = await db_session.get(ProviderCredential, credential_id)
    assert credential is not None
    assert credential.partner_application_id == application_id
    assert credential.partner_id is None
    assert credential.encrypted_api_key is not None
    assert "partner-upstream-secret" not in credential.encrypted_api_key

    master_key = get_settings().provider_credentials_master_key
    assert master_key is not None
    assert decrypt_secret(credential.encrypted_api_key, master_key) == "partner-upstream-secret"


async def test_application_approval_moves_provider_credential_to_partner(
    client,
    db_session,
    admin_headers,
    monkeypatch,
):
    class ValidatingAdapter:
        async def validate_key(self, api_key: str) -> bool:
            return True

    monkeypatch.setattr(
        "app.providers.router.get_provider_adapter",
        lambda provider, *, api_key=None: ValidatingAdapter(),
    )

    application = await client.post(
        "/api/v1/accounts/applications",
        json={
            "telegram_id": "provider-credential-approval",
            "company_name": "Credential Approval Partner",
            "project_name": "Credential Approval Bot",
        },
    )
    assert application.status_code == 201
    application_id = application.json()["id"]

    credential_response = await client.post(
        "/api/v1/providers/credentials",
        headers=admin_headers,
        json={
            "provider": "argolink",
            "label": "primary",
            "api_key": "approval-upstream-secret",
            "partner_application_id": application_id,
        },
    )
    assert credential_response.status_code == 201

    approval = await client.post(
        f"/api/v1/accounts/applications/{application_id}/approve",
        headers=admin_headers,
    )
    assert approval.status_code == 200
    partner_id = approval.json()["id"]

    credential = await db_session.get(ProviderCredential, credential_response.json()["id"])
    assert credential is not None
    assert credential.partner_application_id == application_id
    assert credential.partner_id == partner_id
    assert credential.is_active is True
