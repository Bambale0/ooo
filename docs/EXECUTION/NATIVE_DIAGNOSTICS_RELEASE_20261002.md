# Native upstream diagnostics release

Baseline: 630ce82633819ac923f4eb6c1035e72afb888e38.
Scope: publish the already implemented metadata-only NativeRequestTrace. User requested production diagnostics on 2026-10-02. The later request for sanitized payload previews remains UNIMPLEMENTED: its source-write operation was blocked by the platform safety gate. This release does not contain that blocked implementation. Its new RED test remains in the original development worktree, not in this release.

Problem reproduced: an upstream HTTP 524 was mapped to 503 submission_outcome_unknown without durable upstream status/CF-Ray/timing/error-category evidence. New regression failed on empty ProviderAttempt.raw_error. Minimal fix records only allowlisted context, status, timing, CF-Ray/UUID and exception class. No prompts, URLs, credentials or response bodies are collected. No retries, pricing, balance changes, storage/ACK or new network routes. No claim to fix the upstream timeout.

Verification before commit: 96 passed (native API diagnostics, native inference, image model contracts). Full suite and exact-SHA CI remain required. No migration/dependency changes. Negative tests cover credential/header/message exclusion, invalid diagnostic header shape, unchanged public responses, unchanged wallet outcomes, JSON/refs fidelity and one paid submit despite repeated partner requests.

Production topology: project ooo; /opt/neironych/docker-compose.prod.yml + compose.host.yml; 127.0.0.1:18000 -> 8000; immutable current image neironych:630ce82633819ac923f4eb6c1035e72afb888e38; json-file rotation 3 x 10 MiB. Preserve topology, proxy/egress and secrets. Repository automatic deployment uses a different project/port layout: do not arm it blindly. Rollback restores the prior application image; no database change is needed.

Guidance: repository AGENTS, initialized .agents at e4a2e491647fb9d88a61e50f1f05f5059235f09f; systematic-debugging, previously reviewed TDD and release discipline. Separate code review confirmed metadata is internal, raw_error bounded by fixed context, and the application response/state machine are unchanged.
