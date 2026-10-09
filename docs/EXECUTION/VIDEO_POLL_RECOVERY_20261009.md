# Known Argo video polling recovery

## Baseline and evidence

Upstream source: `a80edbcbddfff6057e7d910a27316ee60e0da2d0`.
The local checkout is a connector-exported, Git-blob-checksummed source snapshot,
not an upstream Git clone. Only the resulting patch may be applied to real history.
No production state or specific customer task was inspected during this repair.

Reproduced defects:

1. Status-read errors on an accepted Argo video could exhaust ordinary retries,
   mark the generation failed and release its reserve without a terminal provider result.
2. A completed known video with temporarily missing billing usage entered an
   accounting review that the worker never revisited.
3. Recovered active videos could retain the stale public unknown-submission error.
4. Argo poll responses could carry another task ID without being rejected.

## Repair scope

- Keep accepted Argo video status reads retryable with bounded backoff, independently
  of submission retry limits. Preserve task identity and never submit another paid
  request as part of this read-error recovery.
- Schedule only Argo **video** accounting reviews carrying an existing task ID and
  `usage_reconciliation_required`. Native text/image IDs, missing IDs, unrelated
  review reasons and fallback-provider reviews are outside this automatic lane.
- Keep old accounting-review holds through transient errors and nonterminal replies.
  Do not apply the ordinary age timeout to an accounting review. Resolve it only
  from an explicitly matching terminal task response and valid usage where required.
- Reject any present mismatched/malformed response `request_id`. An absent identity
  preserves the ordinary adapter contract, but cannot authorize accounting-review
  recovery or clear a stale unknown-submission error.
- Clear the stale public error only on a positively identity-verified active response.

## Preserved boundaries and limitations

No schema, price, credential or financial-timeout configuration changes. Existing
ordinary processing timeout and confirmed terminal settlement policies are unchanged.
No live provider submissions, refunds, customer messages or production operations.

The existing durable pre-submit intent already prevents blind replay after a crash.
It cannot reconstruct an Argo ID never received or never durably committed. A 503
without a known upstream ID remains uncertain. The current official Argo contract
provides status by its returned request ID; no client-ID lookup or upstream
idempotent replay contract was verified. This patch does not fabricate that capability.

## Verification

- Initial regression run on unchanged code: **13 failed, 5 passed** for the expected
  behavioral defects (no collection/setup failures).
- Expanded recovery regression suite: **35 passed**, including fresh-worker
  reconstruction, exactly-once success/failure settlement, old accounting holds,
  identity mismatch, text/image isolation and no-ID exclusions.
- Final focused provider, accounting, native API, worker and fault suites:
  **167 passed, 4 skipped**. The skips require a PostgreSQL integration database.
- The final 35-test regression suite was also run against the unchanged source
  snapshot: **23 failed, 12 passed**, confirming defect detection after test expansion.
- Ruff on application and exported tests, and `git diff --check`, pass.

Independent review also reproduced a manual fallback-poll regression in an
intermediate candidate; it was corrected and regression-tested. Missing/malformed
usage keeps durable backoff and cannot silently leave its financial hold.

Full repository CI and PostgreSQL concurrency tests must run on the real-history
branch before release. No publication or deployment is authorized by this document.
