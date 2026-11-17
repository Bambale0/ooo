"""Tests for Partner Dashboard API endpoints."""

import pytest


class TestDashboardProfile:
    """Tests for profile endpoints."""

    async def test_get_profile_success(self, client, test_partner, auth_headers):
        """Should return partner profile."""
        response = await client.get("/v1/dashboard/profile", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["partner_id"] == test_partner.id


class TestDashboardBalance:
    """Tests for balance endpoints."""

    async def test_get_balance(self, client, auth_headers):
        """Should return detailed balance."""
        response = await client.get("/v1/dashboard/balance", headers=auth_headers)
        assert response.status_code == 200
        assert "balance_rub" in response.json()


class TestDashboardApiKeys:
    """Tests for API keys management."""

    async def test_list_api_keys(self, client, auth_headers):
        """Should return partner's API keys."""
        response = await client.get("/v1/dashboard/api-keys", headers=auth_headers)
        assert response.status_code == 200

    async def test_create_api_key_success(self, client, auth_headers):
        """Should create new API key."""
        response = await client.post(
            "/v1/dashboard/api-keys",
            headers=auth_headers,
            json={"name": "Test Key"}
        )
        assert response.status_code == 201
        assert "api_key" in response.json()
