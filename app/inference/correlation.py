import re
from uuid import UUID

from app.generations.models import Generation

_DISTRIBUTOR_IDEMPOTENCY_KEY = re.compile(
    r"^generation:([0-9a-fA-F-]{36}):provider:[0-9]+$"
)


def derive_client_request_id(idempotency_key: str) -> str | None:
    match = _DISTRIBUTOR_IDEMPOTENCY_KEY.fullmatch(idempotency_key)
    if match is not None:
        try:
            return str(UUID(match.group(1)))
        except ValueError:
            pass
    return None


def generation_client_request_id(generation: Generation) -> str | None:
    stored = (generation.request_payload or {}).get("client_request_id")
    if isinstance(stored, str) and stored:
        return stored
    return derive_client_request_id(generation.idempotency_key)
