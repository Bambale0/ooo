"""Durable, encrypted pool of pre-issued ArgoLink keys for self-service onboarding."""

import json
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.models import Partner, PartnerApplication
from app.infrastructure.config import get_settings
from app.infrastructure.security import decrypt_secret, encrypt_secret, hash_secret
from app.providers.base import ProviderAdapterError
from app.providers.models import ProviderCredential, ProviderKeyPoolItem
from app.providers.registry import get_provider_adapter


def parse_key_list(value: str) -> list[str]:
    """Parse a JSON array without ever including a secret in an error."""
    try:
        raw = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(503, "provider_key_pool_configuration_invalid") from exc
    if not isinstance(raw, list):
        raise HTTPException(503, "provider_key_pool_configuration_invalid")
    if not raw:
        return []
    keys: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise HTTPException(503, "provider_key_pool_configuration_invalid")
        key = item.strip()
        if len(key) < 8 or any(character.isspace() for character in key):
            raise HTTPException(503, "provider_key_pool_configuration_invalid")
        keys.append(key)
    if len({hash_secret(key) for key in keys}) != len(keys):
        raise HTTPException(409, "provider_key_pool_duplicate")
    return keys


def parse_admin_key_lines(value: str) -> list[str]:
    keys = [line.strip() for line in value.splitlines() if line.strip()]
    if not keys or any(len(key) < 8 or any(character.isspace() for character in key) for key in keys):
        raise HTTPException(422, "provider_key_pool_input_invalid")
    if len({hash_secret(key) for key in keys}) != len(keys):
        raise HTTPException(409, "provider_key_pool_duplicate")
    return keys


async def _validate_keys(keys: list[str]) -> None:
    # Validate every item before the first database write: a mixed valid/invalid
    # message is rejected atomically and never leaves a partial pool.
    for key in keys:
        adapter = get_provider_adapter("argolink", api_key=key)
        if not await adapter.validate_key(key):
            raise HTTPException(409, "provider_key_invalid")
        try:
            await adapter.prepaid_balance_usdt()
        except ProviderAdapterError as exc:
            # Runtime capital admission currently supports only unrestricted
            # prepaid USD/USDT wallets. Quota/standard keys must not activate a
            # partner whose every generation would then fail closed.
            raise HTTPException(409, "provider_key_mode_unsupported") from exc


async def _existing_hashes(db: AsyncSession, hashes: list[str]) -> set[str]:
    if not hashes:
        return set()
    pooled = await db.scalars(select(ProviderKeyPoolItem.key_hash).where(ProviderKeyPoolItem.key_hash.in_(hashes)))
    assigned = await db.scalars(select(ProviderCredential.key_hash).where(ProviderCredential.key_hash.in_(hashes)))
    return set(pooled).union(assigned)


async def lock_key_hashes(db: AsyncSession, hashes: list[str]) -> None:
    """Serialize cross-table ownership changes for provider keys on PostgreSQL."""
    if db.get_bind().dialect.name != "postgresql":
        return
    # Both the pool and manual-credential writers acquire the same sorted set
    # of transaction-scoped locks. This closes the check-then-insert race even
    # though the two owners live in separate tables.
    for key_hash in sorted(set(hashes)):
        lock_id = int(key_hash[:16], 16)
        if lock_id >= 2**63:
            lock_id -= 2**64
        await db.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": lock_id})


async def validate_new_pool_keys(db: AsyncSession, keys: list[str]) -> None:
    normalized = [key.strip() for key in keys]
    if not normalized or len({hash_secret(key) for key in normalized}) != len(normalized):
        raise HTTPException(409, "provider_key_pool_duplicate")
    await _validate_keys(normalized)
    if await _existing_hashes(db, [hash_secret(key) for key in normalized]):
        raise HTTPException(409, "provider_key_pool_duplicate")


async def add_pool_keys(
    db: AsyncSession,
    keys: list[str],
    *,
    source: str,
    actor: str,
    allow_existing_pool_items: bool = False,
) -> int:
    settings = get_settings()
    if not settings.provider_credentials_master_key:
        raise HTTPException(503, "provider_credentials_encryption_not_configured")
    normalized = [key.strip() for key in keys]
    if not normalized or len({hash_secret(key) for key in normalized}) != len(normalized):
        raise HTTPException(409, "provider_key_pool_duplicate")
    await _validate_keys(normalized)
    hashes = [hash_secret(key) for key in normalized]
    await lock_key_hashes(db, hashes)
    existing = await _existing_hashes(db, hashes)
    if existing and not allow_existing_pool_items:
        raise HTTPException(409, "provider_key_pool_duplicate")
    pending = [(key, key_hash) for key, key_hash in zip(normalized, hashes, strict=True) if key_hash not in existing]
    if not pending:
        return 0
    rows = [
        ProviderKeyPoolItem(
            provider="argolink",
            key_hash=key_hash,
            encrypted_api_key=encrypt_secret(key, settings.provider_credentials_master_key),
            status="available",
            source=source,
            added_by=actor,
        )
        for key, key_hash in pending
    ]
    try:
        async with db.begin_nested():
            db.add_all(rows)
            await db.flush()
    except IntegrityError as exc:
        # An administrator duplicate is an error. Concurrent idempotent env
        # import is accepted only when every hash now exists in the pool.
        if not allow_existing_pool_items or not set(hashes).issubset(await _existing_hashes(db, hashes)):
            raise HTTPException(409, "provider_key_pool_duplicate") from exc
        return 0
    return len(rows)


async def bootstrap_configured_pool(db: AsyncSession) -> int:
    secret = get_settings().argolink_registration_key_pool_json
    if secret is None:
        return 0
    keys = parse_key_list(secret.get_secret_value())
    if not keys:
        return 0
    hashes = [hash_secret(key) for key in keys]
    existing = await _existing_hashes(db, hashes)
    # A key already bound outside the pool must never be silently imported as
    # another available item. Existing pool rows make env bootstrap replay-safe.
    pooled = set(
        await db.scalars(select(ProviderKeyPoolItem.key_hash).where(ProviderKeyPoolItem.key_hash.in_(hashes)))
    )
    if existing - pooled:
        raise HTTPException(503, "provider_key_pool_configuration_invalid")
    missing = [key for key in keys if hash_secret(key) not in pooled]
    return await add_pool_keys(
        db,
        missing,
        source="environment",
        actor="production-secret",
        allow_existing_pool_items=True,
    ) if missing else 0


async def provision_application_key(db: AsyncSession, application_id: str) -> ProviderCredential:
    application = await db.get(PartnerApplication, application_id, with_for_update=True)
    if application is None:
        raise HTTPException(404, "application_not_found")
    # One Telegram identity must not drain a new pool key through concurrent or
    # delete/re-register applications. Applications retain the original
    # Telegram id even after a deleted Partner is anonymized.
    await lock_key_hashes(db, [hash_secret(f"partner-registration:{application.telegram_id}")])
    existing = await db.scalar(
        select(ProviderCredential).where(
            ProviderCredential.provider == "argolink",
            ProviderCredential.partner_application_id == application_id,
            ProviderCredential.is_active.is_(True),
            ProviderCredential.encrypted_api_key.is_not(None),
        )
    )
    if existing is not None:
        return existing
    reusable = await db.scalar(
        select(ProviderCredential)
        .where(
            ProviderCredential.provider == "argolink",
            ProviderCredential.is_active.is_(True),
            ProviderCredential.encrypted_api_key.is_not(None),
            ProviderCredential.registration_telegram_id == application.telegram_id,
        )
        .order_by(ProviderCredential.created_at.desc())
        .with_for_update(of=ProviderCredential)
        .limit(1)
    )
    if reusable is not None:
        previous_partner = await db.get(Partner, reusable.partner_id)
        reusable.partner_application_id = application.id
        if previous_partner is not None and previous_partner.status == "deleted":
            reusable.partner_id = None
        await db.execute(
            update(ProviderKeyPoolItem)
            .where(ProviderKeyPoolItem.key_hash == reusable.key_hash)
            .values(
                assigned_application_id=application.id,
                assigned_partner_id=reusable.partner_id,
            )
        )
        await db.flush()
        return reusable
    settings = get_settings()
    # Serialize the global rolling budget in production so rotating Telegram
    # identities cannot race past the operator-funded onboarding ceiling.
    await lock_key_hashes(db, [hash_secret("argolink-registration-claim-budget")])
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.argolink_registration_claim_window_seconds)
    recent_claims = await db.scalar(
        select(func.count())
        .select_from(ProviderKeyPoolItem)
        .where(
            ProviderKeyPoolItem.provider == "argolink",
            ProviderKeyPoolItem.status == "assigned",
            ProviderKeyPoolItem.assigned_at >= cutoff,
        )
    )
    if int(recent_claims or 0) >= settings.argolink_registration_claim_budget:
        raise HTTPException(503, "provider_key_registration_budget_exhausted")
    await bootstrap_configured_pool(db)
    claimed = None
    # PostgreSQL skips rows already being assigned by another registration.
    # The conditional update also keeps SQLite-based tests safe. Exhaustion is
    # reported only after a fresh selection sees no candidate; heavy transient
    # contention is reported as unavailable, never as an empty pool.
    for _ in range(20):
        candidate_query = (
            select(ProviderKeyPoolItem.id)
            .where(
                ProviderKeyPoolItem.provider == "argolink",
                ProviderKeyPoolItem.status == "available",
                ProviderKeyPoolItem.encrypted_api_key.is_not(None),
            )
            .order_by(ProviderKeyPoolItem.created_at, ProviderKeyPoolItem.id)
            .limit(1)
        )
        candidate_id = await db.scalar(candidate_query.with_for_update(skip_locked=True))
        if candidate_id is None:
            # SKIP LOCKED can see no row while another registration temporarily
            # holds the last candidate. The blocking recheck distinguishes that
            # from durable exhaustion and observes a rollback if the peer fails.
            candidate_id = await db.scalar(candidate_query.with_for_update())
            if candidate_id is None:
                break
        claimed = await db.scalar(
            update(ProviderKeyPoolItem)
            .where(
                ProviderKeyPoolItem.id == candidate_id,
                ProviderKeyPoolItem.status == "available",
            )
            .values(
                status="assigned",
                assigned_application_id=application_id,
                assigned_at=func.now(),
            )
            .returning(ProviderKeyPoolItem)
        )
        if claimed is not None:
            break
    if claimed is None and candidate_id is not None:
        raise HTTPException(503, "provider_key_pool_unavailable")
    if claimed is None or not claimed.encrypted_api_key:
        raise HTTPException(503, "provider_key_pool_empty")
    master_key = get_settings().provider_credentials_master_key
    if not master_key:
        raise HTTPException(503, "provider_credentials_encryption_not_configured")
    try:
        plaintext = decrypt_secret(claimed.encrypted_api_key, master_key)
    except ValueError as exc:
        raise HTTPException(503, "provider_key_pool_unavailable") from exc
    credential = ProviderCredential(
        provider="argolink",
        label="Автоматический ключ ArgoLink",
        key_hash=claimed.key_hash,
        key_prefix=plaintext[:8],
        encrypted_api_key=encrypt_secret(plaintext, master_key),
        partner_application_id=application_id,
        registration_telegram_id=application.telegram_id,
    )
    db.add(credential)
    claimed.encrypted_api_key = None
    await db.flush()
    return credential


async def bind_claimed_key_to_partner(db: AsyncSession, application_id: str, partner_id: str) -> None:
    await db.execute(
        update(ProviderKeyPoolItem)
        .where(ProviderKeyPoolItem.assigned_application_id == application_id)
        .values(assigned_partner_id=partner_id)
    )


async def pool_counts(db: AsyncSession) -> tuple[int, int]:
    rows = await db.execute(
        select(ProviderKeyPoolItem.status, func.count())
        .where(ProviderKeyPoolItem.provider == "argolink")
        .group_by(ProviderKeyPoolItem.status)
    )
    counts = dict(rows.all())
    return int(counts.get("available", 0)), int(counts.get("assigned", 0))


async def provision_shared_registration_key(db: AsyncSession) -> ProviderCredential:
    """Promote the explicitly selected pool item once, without consuming it per signup."""
    settings = get_settings()
    pool_id = settings.argolink_registration_shared_pool_item_id
    if not pool_id:
        raise HTTPException(503, "provider_key_pool_configuration_invalid")
    master_key = settings.provider_credentials_master_key
    if not master_key:
        raise HTTPException(503, "provider_credentials_encryption_not_configured")
    # All registrations serialize only promotion/binding, not external provider HTTP.
    item = await db.scalar(
        select(ProviderKeyPoolItem)
        .where(ProviderKeyPoolItem.id == pool_id, ProviderKeyPoolItem.provider == "argolink")
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if item is None or item.status not in {"available", "shared"}:
        raise HTTPException(503, "provider_key_pool_configuration_invalid")
    if item.assigned_partner_id is not None or item.assigned_application_id is not None:
        raise HTTPException(503, "provider_key_pool_configuration_invalid")
    await lock_key_hashes(db, [item.key_hash])
    credential = await db.scalar(select(ProviderCredential).where(ProviderCredential.key_hash == item.key_hash))
    if item.status == "shared":
        if (
            credential is None
            or credential.provider != "argolink"
            or credential.partner_id is not None
            or credential.partner_application_id is not None
            or not credential.is_active
            or not credential.encrypted_api_key
        ):
            raise HTTPException(503, "provider_key_pool_unavailable")
        ciphertext = credential.encrypted_api_key
    else:
        if credential is not None or not item.encrypted_api_key:
            raise HTTPException(503, "provider_key_pool_unavailable")
        ciphertext = item.encrypted_api_key
    try:
        plaintext = decrypt_secret(ciphertext, master_key)
    except ValueError as exc:
        raise HTTPException(503, "provider_key_pool_unavailable") from exc
    if credential is None:
        credential = ProviderCredential(
            provider="argolink",
            label="Shared registration key",
            key_hash=item.key_hash,
            key_prefix=plaintext[:8],
            encrypted_api_key=ciphertext,
        )
        db.add(credential)
        item.status = "shared"
        item.encrypted_api_key = None
        item.assigned_at = func.now()
        await db.flush()
    return credential
