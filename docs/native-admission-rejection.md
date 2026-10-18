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
