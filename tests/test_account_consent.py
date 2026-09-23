async def test_application_rejects_missing_explicit_consents(client, admin_headers):
    response = await client.post(
        "/api/v1/accounts/applications",
        headers=admin_headers,
        json={
            "telegram_id": "consent-missing",
            "company_name": "Consent Missing",
            "project_name": "Consent Bot",
        },
    )

    assert response.status_code == 422


async def test_application_rejects_false_consent(client, admin_headers):
    response = await client.post(
        "/api/v1/accounts/applications",
        headers=admin_headers,
        json={
            "telegram_id": "consent-false",
            "company_name": "Consent False",
            "project_name": "Consent Bot",
            "accepted_terms": True,
            "accepted_privacy_policy": False,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "consent_required"


async def test_application_accepts_explicit_true_consents(client, admin_headers):
    response = await client.post(
        "/api/v1/accounts/applications",
        headers=admin_headers,
        json={
            "telegram_id": "consent-true",
            "company_name": "Consent True",
            "project_name": "Consent Bot",
            "accepted_terms": True,
            "accepted_privacy_policy": True,
        },
    )

    assert response.status_code == 201


async def test_public_caller_cannot_forge_telegram_consent(client):
    response = await client.post(
        "/api/v1/accounts/applications",
        json={
            "telegram_id": "123456",
            "company_name": "Forged",
            "project_name": "Forged",
            "accepted_terms": True,
            "accepted_privacy_policy": True,
        },
    )
    assert response.status_code == 401
