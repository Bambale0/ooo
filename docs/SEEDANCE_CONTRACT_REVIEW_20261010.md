# Seedance 2.5 contract review (2026-10-10)

## Confirmed fixes

- ArgoLink documents `generate_audio: false` for a silent result. The gateway
  incorrectly rejected it. Preserve either boolean unchanged for `seedance-2.5`;
  reject null, numbers, strings and containers before any reservation.
- Validation collapsed all reasons into `422 invalid_request_contract`. Keep
  this existing response body for compatibility, and add `X-Validation-Error`
  only for an explicit allowlist. Structured logs carry the same safe code in
  `validation_reason`, correlated with `X-Request-Id`, partner and API-key IDs.
  Never return/log arbitrary exception messages, prompts, URLs or secrets.
- The partner guide said edit allowed "at least one" video. Align the guide
  with the current explicit single-video admission rule and explain reference
  mode separately. No automatic mode rewrite, removed references or retries.

## Unresolved upstream documentation conflict

Sources checked:

- https://argolink.io/en/docs (Seedance / Edit a video)
- https://argolink.io/en/models/seedance-2.5 (Edit a video; full parameters)

The detailed guide expressly requires one video and says image/audio/additional
video references are rejected with HTTP 400. The model card's generic parameter
table labels the reference arrays "reference and edit", contradicting that
restriction on the same page. The initial mixed-edit acceptance test proposal
was therefore **not a verified requirement** and is replaced with explicit
contract tests. Existing single-video rejection coverage is retained.

A direct live request-shape probe was blocked by the execution tool and was not
re-routed or retried. Neither mock responses nor historical requests awaiting
reconciliation prove upstream mixed-edit support. Do not claim this release
makes photo+video edit requests work. Relax that restriction only after explicit
provider clarification or an authorized successful end-to-end test, including
confirmation that every reference was actually used.

## Verification and financial boundary

- Regression tests reproduce unsupported silent-video rejection, non-boolean
  acceptance and lost validation reasons on the prior runtime.
- API -> durable reservation -> worker -> adapter -> mock upstream -> settlement
  preserves `false` for text, mixed-reference and single-video-edit requests.
- Duplicate submit does not create a second provider job; settlement charges
  actual billed seconds. Rejected requests create no generation/attempt/ledger.
- Reference count limits, edit semantics, reserve bounds, cost-plus pricing,
  snapshots and unknown-submission reconciliation rules remain unchanged.
- No production credentials, customer assets or paid render requests are used
  by the regression suite. Mock integration tests are not live provider smoke.
