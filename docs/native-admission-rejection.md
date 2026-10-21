# Native inference admission rejection receipt

## Explicit opt-in and compatibility

Clients may send `X-Submission-Receipt: v1` with their validated `X-Client-Request-Id` and `Idempotency-Key`. This opts into terminal rejection of a logical key: after a receipted failure, a genuinely new user action must use a new key. Legacy requests without the opt-in retain the original retryable rejection behavior and receive no receipt.

## Durable receipt

For a typed definitive service-unavailable admission error, the gateway first rolls back. It then re-acquires the existing partner lock and rechecks the idempotency key. If an overlapping retry already won admission, the gateway returns that accepted identity instead of a rejection receipt. Otherwise it commits an unbilled terminal generation as the existing durable idempotency tombstone. No provider attempt, customer charge, cost reservation or ledger entry is created. The tombstone prevents that key from ever being accepted later.

Only after this commit does the rejection carry:

```
X-Submission-State: not-accepted-v1
X-Client-Request-Id: <the validated invocation ID>
X-Idempotency-Key-Sha256: <SHA-256 of the supplied idempotency key>
```

The HTTP response remains 503 with `{"detail":"provider_temporarily_unavailable"}`. The receipt is provider-agnostic: credential, pricing, funding and circuit admission rejections use the same state. It does not reveal the internal failure stage, provider balance, routing or credentials.

## Failure semantics

Generic HTTP exceptions, accepted or post-acceptance failures, failed rollback or failed fence persistence never issue no-acceptance authority. Failed persistence is logged with its actual response status, not as a successful 503 receipt. An accepted idempotent replay has no rejection marker. A tombstoned key replays the rejection and never resumes provider work; changed content under that key retains the normal conflict protection.

Callers must verify the version, exact status/body, invocation ID and idempotency digest together over the authenticated provider connection before switching provider. Timeouts, unmarked errors and failed status lookup stay ambiguous. Older clients ignore the receipt safely. Gateway-first rollout is required for the corresponding client fix.

No treasury controls, prices, historical balances or procurement holds are changed. No schema migration is needed; the existing terminal finality and unique partner/idempotency constraints protect the tombstone. Real PostgreSQL regressions exercise ten concurrent fences and a retry that wins admission between rollback and re-locking.

## Storage-abuse boundary

`NATIVE_REJECTION_RECEIPTS_MAX_PER_PARTNER` is a typed infrastructure ceiling in the existing settings system (default 10,000; allowed 0–100,000), following the existing native response size/retention limits. Under the same partner lock, the gateway counts at most this many rejection records before inserting. Rotating logical keys or concurrent requests cannot exceed the per-partner cumulative quota. Other partners have independent budgets. On exhaustion it returns an ordinary unreceipted 503 and writes no tombstone; callers remain fail-closed. Existing rejection/accepted identities are checked first and continue to replay even if new receipt creation is disabled. Old fences are never deleted to reclaim quota, since that would reopen a previously rejected key. This is a storage safety limit, not a tariff or model-availability setting; changing the infrastructure ceiling does not require source edits or a schema migration.
