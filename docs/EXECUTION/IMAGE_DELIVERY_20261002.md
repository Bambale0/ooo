# Temporary recoverable image delivery

Baseline: 630ce82633819ac923f4eb6c1035e72afb888e38.
User request (2026-10-02): eliminate lost image delivery; retain successful images only until confirmed delivery, then delete, with bounded TTL/quota. No blind paid replay.

Evidence: deployed sync images/edits calls to ArgoLink returned HTTP 524; gateway maps them to submission_outcome_unknown. Current public ArgoLink docs expose synchronous images and asynchronous VIDEO only; no verified alternative image endpoint is available. This change cannot claim to remove upstream 524. Do not enable a speculative upstream route or retry an ambiguous paid request.

Existing: durable Generation/reserve/ProviderAttempt, video worker, scoped auth, frozen billing. Missing: disconnect-independent image dispatch, replayable results, explicit delivery ACK, bounded temporary storage. Reuse existing tables/JSON metadata: no migration or historical ledger rewrite.

Approved seams from user outcome: partner HTTP API, worker/provider HTTP boundary, filesystem result lifecycle, KSU delivery boundary. TDD: enqueue -> one upstream submit -> persisted result -> repeated GET -> ACK -> deletion; 524 must remain uncertain without another paid POST. Add tenant isolation, checksum, expiry, restart, concurrent claims, quota and legacy-API regressions.

Design: opt-in Prefer: respond-async for JSON Nano Banana Pro; stable request_id and idempotency lookup. Dedicated image worker using committed submission intent. Private shared local volume, atomic/fsynced result envelope, reserved quota before submit. GET never deletes. ACK verifies digest; tombstone survives deletion. TTL and orphan cleanup are bounded and independent from admission enablement. User instruction supersedes brief section 53 only for temporary delivery spooling, not archival retention.

Rollout: feature OFF until exact-SHA CI and end-to-end staging pass; deploy gateway before KSU, then enable explicitly. Rollback disables new admissions but must keep recovery/cleanup worker and shared volume for existing work. No production mutations in this worktree.

Guidance: repository AGENTS and initialized .agents; systematic-debugging; Bambale0/skills engineering/tdd; claw architecture/configuration discipline; wondelai Release It cleanup/bounded retries; dev-agents-pack verification; AgentSkills/Anthropic catalogs have no additional applicable runtime-specific guidance.

Progress: baseline tests/test_native_inference.py: 31 passed. Next: failing HTTP lifecycle regression.

## Checkpoint / not implemented

The new HTTP lifecycle test ran and failed at the intended first assertion: expected asynchronous admission HTTP 202, received synchronous HTTP 200. Baseline native inference tests remain 31 passed.

Two attempts to apply implementation source were blocked by the tool safety gate (unable to determine safety). A subsequent git status confirmed that NO app source files were changed: only this ledger, context.md, and tests/test_image_jobs.py exist as changes. Do not report storage/ACK/async dispatch or KSU integration as implemented. Do not merge this RED draft or deploy it.

Implementation acceptance includes: ACK only after confirmed delivery, never on GET; SHA-256 receipt; same-key replay cannot create another upstream job after deletion; bounded configurable TTL/quota; quota admission before upstream billing; tenant-isolated lookup/result/ACK; recovery after result fsync but before DB commit; retained tombstone after unlink; deletion intent recovery on restart; TTL cleanup cannot race an active transfer. Existing KSU history/media retention is outside scope: only delivery spool copies are authorized for deletion.

Upstream release blocker: official ArgoLink image documentation currently provides synchronous /v1/images/generations and /v1/images/edits, not a verified async image/result retrieval API or long-request host. Never guess such a host, bypass provider protections, or send credentials to an unverified address.
