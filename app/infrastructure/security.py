import hashlib
import secrets


def create_api_key() -> tuple[str, str]:
    token = f"nrn_{secrets.token_urlsafe(32)}"
    return token, hash_secret(token)


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    return secrets.compare_digest(left, right)
