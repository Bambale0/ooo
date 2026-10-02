# Full UUID tracing for administrators and partners

Baseline: 7ad76c8217bd7f886d406c70b6bd9dd7809b4d5f (current main; includes partner-management PR 68). Production observed: 1cfd6ae31d7c4af2c26cfb94fbe9b07bd12d96a6. Do not overwrite other worktrees.

Request: all relevant UUIDs must be full, visible in administrator and partner logs/history to trace a request.

Evidence: admin generation/payment lists truncate UUIDs to 8 characters; partner ledger history omits both operation and generation IDs. Native generation success has X-Request-Id but 503/409 branches are inconsistent; authentication/validation failures have no correlation identifier. Native metadata omits partner ID. Worker/webhook logs lack uniform UUID context.

Scope: remove UI UUID shortening and expose linked operation IDs; every HTTP call gets a server-issued full UUID, full generation ID remains stable across duplicate submissions, diagnostic context joins partner/API-key-record/generation/attempt/webhook. Partner-scoped read-only trace view and admin-scoped full trace view support exact UUID lookup. Internal upstream IDs remain admin-only per product contract. Never log credentials, prompts, signed URLs or response bodies. No billing, provider retry/routing or media behavior changes.

Verification seams: actual ASGI HTTP requests (success, pre-admission rejection, uncertain failure, duplicate, concurrent requests), Telegram dispatcher user/admin history/search, provider worker and webhook services, JSON formatter. TDD one vertical slice at a time. PostgreSQL/authorization/idempotency coverage via full exact-SHA CI. No schema/dependency changes expected.

Rollout: current main already includes PR 68; review full baseline delta before deploying exact merged SHA. Preserve host compose/ports/relay/log rotation and previous image for rollback. No paid generations or mass notifications for smoke. Diagnostics do not resolve upstream 524.

Guidance: AGENTS, initialized .agents @ e4a2e491647fb9d88a61e50f1f05f5059235f09f, code-showcase-systematic-debugging; product EPIC 22 and launch checklist 24. State starts as not implemented.

## Verified release boundary

The pure-ASGI context implementation and a subsequent native-only identity implementation were both rejected by the platform safety gate. Read-only git verification confirms no API/infrastructure files changed. Do not retry the blocked source writes through another tool. Do not claim a new HTTP identity layer, trace API, partner_id logging or worker/webhook instrumentation was delivered.

The independently applied UI fix satisfies full display of EXISTING operation identifiers: all three `.id[:8]` sites removed; administrator ledger and partner history now include full record UUID and linked operation UUID. Existing native diagnostic code already writes full generation_id and attempt_id, and native responses already carry the persisted request ID in body or header. No new identifier is invented or existing one changed. API-key secrets remain masked.

The UI regression was RED on truncated generation UUID, then GREEN; 23 history/partner tests passed. The broader unimplemented HTTP-correlation test remains unchanged in untracked tests/test_full_uuid_tracing.py in the development worktree; it is NOT part of this UI-only release and remains a known failing deferred acceptance test. Release test tests/test_uuid_history.py preserves the exact UI assertions separately. This is a scoped partial delivery, not evidence that the expanded trace design works.

Release must run all tests from the clean exact commit, not the development tree with pending API work. No tests from the pre-existing repository are disabled, changed or removed.

## UI completion verification

Second regression reproduced: the paid-invoice queue previously contained a UUID only in the button. Full UUID is now present in the message body too. Focused verification: 57 passed (UUID history, Telegram cabinet/security/partner management, existing native diagnostics). Added ownership isolation, full-page Telegram length, callback size and exact-ID search coverage. Only UI rendering and documentation changed; existing API diagnostic metadata remains unchanged.
