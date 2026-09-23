import logging
import re

from pythonjsonlogger.json import JsonFormatter

from app.infrastructure.config import get_settings


class SecretRedactingFormatter(JsonFormatter):
    def __init__(self, *args, secrets=(), **kwargs):
        super().__init__(*args, **kwargs)
        self._secrets = tuple(value for value in secrets if isinstance(value, str) and len(value) >= 8)

    def format(self, record):
        # Redact the final formatted string, including exception tracebacks and extras.
        result = super().format(record)
        for secret in self._secrets:
            result = result.replace(secret, "[REDACTED]")
        result = re.sub(r"nrn_[A-Za-z0-9_-]{30,}", "[REDACTED]", result)
        result = re.sub(r"sk-[A-Za-z0-9_-]{20,}", "[REDACTED]", result)
        result = re.sub(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}", "[REDACTED]", result)
        return result


def configure_logging() -> None:
    settings = get_settings()
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(settings.log_level)
    secrets = [
        settings.admin_api_token,
        settings.argolink_api_key,
        settings.crypto_pay_api_token,
        settings.telegram_bot_token,
        settings.provider_credentials_master_key,
        settings.s3_secret_access_key,
        settings.database_url,
    ]
    handler = logging.StreamHandler()
    handler.setFormatter(
        SecretRedactingFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s %(trace_id)s",
            secrets=secrets,
        )
    )
    root.addHandler(handler)
    for name in ("uvicorn", "uvicorn.error", "httpx", "httpcore", "aiogram"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
