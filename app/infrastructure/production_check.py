"""Fail closed on unsafe production configuration without printing secret values."""

from urllib.parse import urlsplit

from app.infrastructure.config import get_settings


def validate_production_config() -> list[str]:
    settings = get_settings()
    if settings.app_env != "production":
        return []
    errors: list[str] = []
    for name in ("admin_api_token", "provider_credentials_master_key"):
        value = getattr(settings, name) or ""
        if len(value) < 32 or any(part in value.lower() for part in ("change-me", "change-this", "test-", "example")):
            errors.append(f"{name.upper()} must be a unique random secret of at least 32 characters")
    if not settings.database_url.startswith("postgresql+asyncpg://"):
        errors.append("DATABASE_URL must use postgresql+asyncpg in production")
    for name in ("public_api_base_url", "argolink_base_url", "crypto_pay_base_url"):
        url = urlsplit(getattr(settings, name))
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            errors.append(f"{name.upper()} must be an HTTPS URL without credentials")
    if not settings.redis_url.startswith(("redis://", "rediss://")):
        errors.append("REDIS_URL must use redis or rediss")
    if settings.telegram_bot_token:
        if not settings.admin_telegram_id or not settings.admin_telegram_id.isdigit():
            errors.append("ADMIN_TELEGRAM_ID must be the verified administrator's numeric ID")
        for name in ("terms_url", "privacy_policy_url"):
            url = urlsplit(getattr(settings, name) or "")
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                errors.append(f"{name.upper()} must point to the published HTTPS legal document")
    return errors


def require_production_config() -> None:
    errors = validate_production_config()
    if errors:
        raise RuntimeError("Invalid production configuration: " + "; ".join(errors))
