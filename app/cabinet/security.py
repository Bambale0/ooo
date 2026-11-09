"""Telegram Login Widget verification and browser session security helpers."""

import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime

from fastapi import HTTPException

from app.infrastructure.config import get_settings

MAX_TELEGRAM_AUTH_AGE_SECONDS = 300
SESSION_TTL_SECONDS = 24 * 60 * 60
SESSION_COOKIE = "nrn_cabinet_session"
LOGIN_NONCE_COOKIE = "nrn_cabinet_login"


def verify_telegram_login(payload: dict[str, str | int], *, now: datetime | None = None) -> str:
    """Return verified Telegram ID, never a client-provided partner ID."""
    token = get_settings().telegram_bot_token
    if not token or len(payload) > 12:
        raise HTTPException(503 if not token else 401, "telegram_login_unavailable" if not token else "invalid_login")
    supplied_hash = payload.get("hash")
    user_id = str(payload.get("id", ""))
    timestamp = str(payload.get("auth_date", ""))
    if (
        not isinstance(supplied_hash, str)
        or not re.fullmatch(r"[a-fA-F0-9]{64}", supplied_hash)
        or not re.fullmatch(r"[1-9][0-9]{0,19}", user_id)
        or not re.fullmatch(r"[0-9]{10,12}", timestamp)
    ):
        raise HTTPException(401, "invalid_login")
    clock = int((now or datetime.now(UTC)).timestamp())
    age = clock - int(timestamp)
    if age < -30 or age > MAX_TELEGRAM_AUTH_AGE_SECONDS:
        raise HTTPException(401, "expired_login")
    if any(not isinstance(key, str) or not isinstance(value, (str, int)) or isinstance(value, bool)
           for key, value in payload.items()):
        raise HTTPException(401, "invalid_login")
    fields = {key: str(value) for key, value in payload.items() if key != "hash"}
    signed = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hashlib.sha256(token.encode("utf-8")).digest()
    expected = hmac.new(secret, signed.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied_hash.lower()):
        raise HTTPException(401, "invalid_login")
    return user_id


def issue_browser_token() -> tuple[str, str]:
    raw = secrets.token_urlsafe(48)
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def hash_browser_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_secure_cookie() -> bool:
    return get_settings().app_env.lower() not in {"test", "dev", "development", "local"}
