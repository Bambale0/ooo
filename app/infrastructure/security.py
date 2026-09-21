import base64
import hashlib
import secrets

from cryptography.fernet import Fernet, InvalidToken


def create_api_key() -> tuple[str, str]:
    token = f"nrn_{secrets.token_urlsafe(32)}"
    return token, hash_secret(token)


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    return secrets.compare_digest(left, right)


def encrypt_secret(value: str, master_key: str) -> str:
    return _fernet(master_key).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str, master_key: str) -> str:
    try:
        return _fernet(master_key).decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise ValueError("secret_decryption_failed") from exc


def _fernet(master_key: str) -> Fernet:
    digest = hashlib.sha256(master_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))
