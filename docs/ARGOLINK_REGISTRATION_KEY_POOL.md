# Individual ArgoLink keys for self-service registration

Every admitted self-service partner must receive a distinct, pre-issued **unrestricted prepaid** ArgoLink key.
Neironych does not create provider keys through ArgoLink's private browser-console API.

Production self-registration is invite-only (`REGISTRATION_INVITE_REQUIRED=true`). The administrator creates a single-use
deep link in Telegram; only the hash is stored, the invite binds transactionally to the first Telegram identity that opens
it, and the invite is consumed in the same transaction as key assignment. This preserves automatic activation for a
legitimate new partner without allowing unrelated Telegram accounts to drain the finite key pool.

## Production secret bootstrap

`ARGOLINK_REGISTRATION_KEY_POOL_JSON` is an optional one-time bootstrap secret. Its value is a JSON array:

```text
["<first pre-issued key>","<second pre-issued key>"]
```

Do not commit this value, paste it into logs, or put it in a command line. Store it in the production secret manager.
On the next admitted registration, keys whose hashes are not already known are validated using the existing non-billable
protected not-found sentinel and the runtime prepaid-capital contract, encrypted with `PROVIDER_CREDENTIALS_MASTER_KEY`,
and inserted into PostgreSQL. Quota/subscription/standard keys without an unrestricted USD/USDT balance are rejected. The environment import
is hash-idempotent. After the administrator screen confirms the expected free count, remove imported plaintext values from
the environment and redeploy; encrypted database rows remain available.

Updating the secret with additional pre-issued keys safely replenishes the pool. Removing values never deletes pool rows.
Duplicate, malformed or invalid values reject the entire missing batch.

## Telegram administration

`Администрирование` → `Ключи ArgoLink для регистраций` shows only counts of free and assigned keys and invite counts. It never shows a key
or prefix. `Добавить ключи` accepts one key per line. The source Telegram message is deleted best-effort; all keys are
validated before any row is written, encrypted inside the durable confirmation action, and validated again when the
administrator confirms. Mixed valid/invalid and duplicate batches are all-or-nothing.

`Создать приглашение` displays one single-use link once. Active invites can be revoked by their non-secret identifier.
Used, revoked and active counts remain auditable without storing the plaintext token.

## Assignment guarantees

- PostgreSQL is the source of truth. A conditional `UPDATE ... RETURNING` claims one available row atomically.
- `key_hash`, and the assignment's application ID, are unique. Replaying the same registration returns its existing
  credential instead of consuming another key.
- Assignment, partner activation, encrypted `ProviderCredential`, consents and the internal `nrn_` key commit in the same
  transaction. A rollback returns the pool row and its encrypted secret to `available`.
- After assignment the pool copy of the ciphertext is erased; the partner credential retains the encrypted secret.
- Partners activated through this path are marked as requiring a dedicated ArgoLink credential. Revoking or losing it
  fails closed and never falls back to the shared `ARGOLINK_API_KEY`. Existing partners are unchanged.
- `ARGOLINK_SHARED_FALLBACK_ENABLED=false` supports a dedicated-only deployment after operators confirm that every live
  legacy partner has an individual credential; while it is true, production still requires `ARGOLINK_API_KEY`.
- An empty or unavailable pool leaves the application pending, creates no partner/internal key, and enqueues one durable,
  deduplicated administrator notification per application. `/start` retries the same application after replenishment.

No validation or test performs a paid generation.
