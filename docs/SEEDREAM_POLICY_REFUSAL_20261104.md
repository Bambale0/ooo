# Seedream image policy refusal, 2026-11-04

Runtime baseline: 8b0d124a15c9492ef418d835782e8674c210af71.

A replay of KSU's failed admin image request returned upstream HTTP 200 with only an error object: code content_policy_violation, no data. Native validation instead raised invalid_image_count and returned provider_response_invalid HTTP 503. The last correlated gateway request was dc136926-12e6-425d-8407-eb534bf2484e.

Recognize the exact structured image error code before inspecting image data. Return terminal HTTP 422 content_policy_violation; never echo upstream messages, prompts, references or provider metadata. Save the terminal response for identical-key replay, do not resubmit, debit or automatically fall back on a policy refusal. Even an error envelope with image data must not deliver that image.

A policy refusal may happen after provider computation. Preserve the existing unknown procurement obligation rather than declaring the upstream call free. Partner billing remains not_chargeable with no ledger debit.

Regression: four HTTP cases reproduced 503 instead of 422 before the fix; after the fix 134 image/Seedream contract, format, safety and replay tests passed in an isolated SQLite container with HTTP blocked. Unknown/malformed envelopes retain ambiguous 503 behavior. New diagnostics persist only the static content_policy_violation reason. KSU companion change adds safe Russian user copy; its existing reject classification closes and refunds the task without fallback.

Guidance: repository AGENTS.md, project .agents systematic-debugging and verification-before-completion; provider abstraction, idempotency, append-only financial records and no raw payload logging. No schema, pricing or route changes.
