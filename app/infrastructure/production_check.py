"""Production configuration validation — run at startup to catch misconfiguration."""

from app.infrastructure.config import get_settings


def validate_production_config() -> list[str]:
    """Check production configuration and return a list of warnings/errors."""
    errors: list[str] = []
    settings = get_settings()

    if settings.app_env == "production":
        if not settings.admin_api_token or settings.admin_api_token == "change-me":
            errors.append("ADMIN_API_TOKEN must be set in production")

        if not settings.database_url or "postgresql" not in settings.database_url:
            errors.append("DATABASE_URL must be set to PostgreSQL in production")

        if not settings.argolink_api_key:
            errors.append("ARGOLINK_API_KEY must be set in production")

        if not settings.provider_credentials_master_key or \
           len(settings.provider_credentials_master_key) < 32:
            errors.append("PROVIDER_CREDENTIALS_MASTER_KEY must be 32+ characters")

        if not settings.redis_url or settings.redis_url == "redis://localhost:6379/0":
            # Allow default for now if user set it correctly
            pass

        if settings.media_storage_backend not in ("s3", "local"):
            errors.append("MEDIA_STORAGE_BACKEND must be 's3' or 'local'")

    return errors