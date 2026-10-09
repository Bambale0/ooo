# Final client release for unconfirmed video submission

Owner decision: 2026-10-09. Policy: `unconfirmed_video_30m_v1`.

## Eligibility and clock

Only newly admitted video requests enroll in this policy. Historical rows remain
NULL; migration does not update or refund them. A historical backfill requires a
separate approved selection and audited reconciliation.

The 30-minute deadline is persisted with the provider submit intent, before the
external request. It is not measured from queue admission. A known accepted task
ID clears the deadline. Safe rejection clears it before a genuinely new attempt;
an unresolved attempt or worker restart never extends it.

At the deadline the worker locks/reloads Generation, then its owner account. It
requires an enrolled, unsettled, nonterminal job with exactly one active no-ID
`submitting` or `reconciliation_required` attempt. Known accepted work, active
retries or competing fallback attempts are excluded. A crashed submit with its
durable intent still present is eligible even before an error message is saved.

## Money and execution are independent

The worker returns the client reserve with the existing canonical release ledger
key and atomically records `client_reserve_released_at`. It does not mark the
execution failed, cancel provider work, clear provider obligations, or launch a
new paid request. Procurement reserve, credential identity, generation UUID and
reconciliation history remain intact.

`actual_charge_rub` remains NULL until verified usage settlement. After a late
success, the result is delivered and client actual charge becomes zero. Provider
actual cost and usage settle normally through their separate ledger. No client
late-charge or usage-adjustment entry is made. Other timeout policies retain
their previous late-settlement behavior.

## Public contract

After final release, `GET /v1/videos/{id}` adds:

- `financial_status: "released_final"`
- `charged_rub: "0.00"`

Execution `status` remains `pending` while acceptance/result is unresolved. The
existing error may still explain the uncertainty. If a result later completes,
status becomes `done` with the normal `video` and `usage`, while both financial
fields remain. Final release is not an execution failure or an `expired` result.
Old jobs and enrolled jobs without a final release retain the existing native
GET shape. Legacy reads and later terminal webhooks expose the same finality.

APIX tolerates these additive fields and can still deliver a late result. Its own
end-user credit policy is separate: this upstream change does not refund APIX
customer credits automatically or alter APIX terminal-state semantics.

## Rollout and rollback

The migration adds nullable enrollment/deadline/finality columns, an index and a
PostgreSQL ledger guard. There is no data backfill. The guard prevents new
generation-linked client ledger entries once final release is recorded; the
canonical release itself precedes that marker in the same transaction. Provider
coverage ledger and unrelated account transactions are unaffected.

This guard is intentionally retained during application rollback: an older app
must fail its attempted late client settlement rather than undo a final waiver.
Its transaction rolls back, including any preceding balance mutation. Such a
rollback can delay result reconciliation until compatible code is restored;
operators must treat this as a billing compatibility issue, not disable the
guard. Schema downgrade refuses to remove finality once any final release exists.

Release gates: full CI, migration/ORM checks, PostgreSQL races (refund/refund,
refund/success, refund/failure), old-settlement ledger rejection, independent
billing review, worker resilience and additive APIX contract review. Production
activation and any historical backfill are distinct from local test execution.
