import pytest

from app.infrastructure.config import Settings
from app.infrastructure.production_check import require_production_config, validate_production_config


def test_unsafe_production_defaults_fail_before_serving(monkeypatch):
    settings = Settings(app_env="production", admin_api_token="change-me", _env_file=None)
    monkeypatch.setattr("app.infrastructure.production_check.get_settings", lambda: settings)
    with pytest.raises(RuntimeError, match="Invalid production configuration"):
        require_production_config()


def test_per_partner_credentials_do_not_require_global_provider_key(monkeypatch):
    settings = Settings(
        app_env="production",
        database_url="postgresql+asyncpg://localhost/test",
        admin_api_token="a" * 40,
        provider_credentials_master_key="b" * 40,
        public_api_base_url="https://api.example.com",
        argolink_api_key=None,
        _env_file=None,
    )
    monkeypatch.setattr("app.infrastructure.production_check.get_settings", lambda: settings)
    assert validate_production_config() == []


async def test_production_readiness_requires_redis(client, monkeypatch):
    from app.infrastructure import state
    from app.infrastructure.config import get_settings

    monkeypatch.setattr(get_settings(), "app_env", "production")
    monkeypatch.setattr(state, "APP_REVISION", "candidate-revision")

    class OfflineRedis:
        async def ping(self):
            return False

        async def close(self):
            pass

    monkeypatch.setattr("app.health.router.create_redis_client", OfflineRedis)
    response = await client.get("/api/v1/readiness")
    assert response.status_code == 503
    assert response.json()["revision"] == "candidate-revision"
