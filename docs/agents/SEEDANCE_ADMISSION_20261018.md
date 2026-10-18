# Seedance / Qwen rejected admission recovery

Baseline: 50963d35033497d5014b830bf3a1eda263d72402. APIX baseline: 0da8798d42dafbe2ba721bebcb85fe186605026b.
Evidence: production native admission rejects Seedance 2.5 and Qwen image requests with HTTP 503/provider_capital before generation creation. APIX treats every 503 as an unknown paid POST and suppresses its existing backup.
Hypotheses checked: provider executing slowly (not supported for admission failures); insufficient effective procurement capacity (confirmed by capital-stage logs); response classification mismatch (confirmed by adapter code).
Outcome: a versioned, correlated no-acceptance receipt only after rollback of a typed admission rejection, allowing the downstream existing equivalent fallback. No relaxation of treasury controls, no historical hold mutation, no new provider or price, no database migration. Generic 503/timeouts and already accepted idempotent requests must remain ambiguous.
Test seams: HTTP admission with real repository/session and mocked upstream; accepted duplicate; generic/post-acceptance error; rollback failure. APIX verifies receipt identity and keeps durable fallback admission.
Steps: [x] logs and existing architecture; [x] repository/local testing guidance; [ ] RED regression; [ ] minimal fix; [ ] full relevant tests/review; [ ] exact-head CI/PR; [ ] merge-SHA autodeploy and runtime verification.

Verification: RED reproduced missing receipts in both video and chat. After minimal router fix, 103 focused tests passed, one PostgreSQL-dependent test skipped locally. Accepted replays, generic/post-acceptance 503 and rollback failure never carry the receipt. Existing treasury policy and balances unchanged. Code/spec self-review found no remaining defect in this patch; exact-head CI and independent review remain required.
