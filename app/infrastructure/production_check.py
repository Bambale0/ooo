"""Fail closed on unsafe production configuration without printing secret values."""

from urllib.parse import urlsplit
from uuid import UUID

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
    for name in ("public_api_base_url", "argolink_base_url", "crypto_pay_base_url", "lava_top_base_url"):
        url = urlsplit(getattr(settings, name))
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            errors.append(f"{name.upper()} must be an HTTPS URL without credentials")
    if settings.payment_provider == "lava_top":
        if not settings.lava_top_api_key:
            errors.append("LAVA_TOP_API_KEY is required when PAYMENT_PROVIDER=lava_top")
        if not settings.lava_top_offer_id:
            errors.append("LAVA_TOP_OFFER_ID is required when PAYMENT_PROVIDER=lava_top")
        else:
            try:
                UUID(settings.lava_top_offer_id)
            except ValueError:
                errors.append("LAVA_TOP_OFFER_ID must be a UUID")
        webhook_secret = (
            settings.lava_top_webhook_secret.get_secret_value() if settings.lava_top_webhook_secret else ""
        )
        if len(webhook_secret) < 32:
            errors.append("LAVA_TOP_WEBHOOK_SECRET must be a random secret of at least 32 characters")
        for name in (
            "lava_top_successful_return_url",
            "lava_top_failure_return_url",
            "lava_top_cancel_return_url",
        ):
            value = getattr(settings, name)
            if value:
                url = urlsplit(value)
                if len(value) > 512 or url.scheme != "https" or not url.hostname or url.username or url.password:
                    errors.append(f"{name.upper()} must be an HTTPS URL without credentials")
    if not settings.redis_url.startswith(("redis://", "rediss://")):
        errors.append("REDIS_URL must use redis or rediss")
    if settings.telegram_bot_token:
        if not settings.registration_invite_required:
            errors.append("REGISTRATION_INVITE_REQUIRED must be true for production Telegram registration")
        if not settings.admin_telegram_id or not settings.admin_telegram_id.isdigit():
            errors.append("ADMIN_TELEGRAM_ID must be the verified administrator's numeric ID")
        if settings.argolink_shared_fallback_enabled and not settings.argolink_api_key:
            errors.append("ARGOLINK_API_KEY is required for legacy partners without a dedicated provider credential")
        username = (settings.telegram_bot_username or "").lstrip("@")
        if not username or not username.replace("_", "").isalnum():
            errors.append("TELEGRAM_BOT_USERNAME is required for referral links")
        for name in ("terms_url", "privacy_policy_url"):
            url = urlsplit(getattr(settings, name) or "")
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                errors.append(f"{name.upper()} must point to the published HTTPS legal document")
    return errors


def require_production_config() -> None:
    errors = validate_production_config()
    if errors:
        raise RuntimeError("Invalid production configuration: " + "; ".join(errors))
