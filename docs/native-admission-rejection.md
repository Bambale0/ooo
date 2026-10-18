# Native inference admission rejection receipt

A native capital-stage rejection can be distinguished from an HTTP 503 after upstream submission. After `reserve()` checks existing request identity under the partner row lock and raises `InferenceAdmissionError` at `provider_capital`, the router completes rollback and may return these response headers:

```
X-Submission-State: not-accepted-v1
X-Client-Request-Id: <the validated invocation ID>
X-Idempotency-Key-Sha256: <SHA-256 of the supplied idempotency key>
```

The status and body are unchanged: HTTP 503 and `{"detail":"provider_temporarily_unavailable"}`. The receipt attests that this admission did not create accepted generation work. It is not emitted for a generic HTTPException, another admission stage, post-acceptance failures, accepted idempotent replay, or failed rollback. Legacy requests without a correlated client ID do not get a receipt.

Callers must verify the version, status/body, invocation ID and idempotency digest together over their authenticated provider connection before switching provider. Unmarked responses, timeouts and failed lookups remain ambiguous. Do not replay an already accepted generation. A rejected request may be retried with its original identity under the normal idempotency contract.

This contract does not disable funding checks, alter balances, clear procurement holds, expose supplier credentials or change prices. No database migration is required. Deploying this gateway first is backward-compatible: older clients ignore the receipt. New clients must remain fail-closed until they receive it.
