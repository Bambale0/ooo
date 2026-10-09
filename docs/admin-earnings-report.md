# Twice-daily admin usage and earnings

The existing `neironych-admin-model-stats.timer` runs at 09:05 and 21:05
Europe/Moscow and covers closed [09:00, 21:00) / [21:00, 09:00) windows.
Do not add a second scheduler or change recipients. The admin ID and notification
outbox are the same application configuration/database used by the bot.

## Meaning of the amounts

* Completed-job revenue is the net of that partner's generation ledger entries
  through the window end. It is not invoice deposits, wallet balances or open holds.
* Cost is the saved `actual_provider_cost_usdt` multiplied by the job's immutable
  `rub_per_usdt_snapshot`, rounded per generation with Decimal/ROUND_HALF_UP.
  A generation total already includes confirmed fallback costs; do not add its
  attempt costs a second time.
* Earnings = the contribution margin on financially accounted completed jobs
  minus known costs of unsuccessful jobs associated with the same report window.
  Missing cost is never silently replaced with zero. A genuinely recorded zero
  cost remains a valid zero. Missing financial fields are disclosed; unpriced
  revenue is not included in earnings.
* This is an operational contribution-margin report before taxes, payment fees,
  infrastructure and other overhead. It is not net profit, a cash-flow statement,
  or permission/available balance to withdraw money.
* Unknown submission/attempt costs and unreconciled retry history make earnings
  provisional and are explicitly disclosed. Old unsettled costs from other
  periods are not a new expense of this window merely because a timer ran.
* Provider actual costs are the latest reconciled values at report construction.
  Late cost confirmation may revise an old window if manually rebuilt; previously
  sent messages are not silently rewritten or re-enqueued. A current tariff
  difference flags historical pricing for review; it never reprices old jobs.
  Existing incorrect accounting snapshots remain a source-data limitation.

## Usage counts

One completed generation UUID is counted once. Prefer its first durable usage
settlement timestamp (legacy charge if present), then first provider success.
This also captures late success after a recorded timeout/error; creation/submission
is not completion. End timestamps are exclusive. Failed attempts are not extra
completed generations. Ready-video seconds are provider output seconds, never
input-video seconds, requested duration, or combined billed seconds. InfAI's
existing provider-reported-duration fallback is retained. Unknown output duration
is visibly unknown.

## Delivery and safe installation

`ops/reports/admin_model_stats.py` is the tracked source of the existing host
script `/usr/local/libexec/neironych/admin_model_stats.py`. The current wrapper
`/usr/local/sbin/neironych-admin-model-stats` passes that script into the app
container using `docker exec -i ... python -`. Installing only the report does
not require a database migration, application restart, or billing changes.

Before install: review/CI, save the old file and its hash, stage the exact reviewed
source, verify its hash, then run it with `--dry-run` inside the production app
container. Independently compare counts and money with read-only SQL. Atomically
replace only the report file after verifying the old hash is still unchanged.
Keep the original wrapper, timer, schedule and service. Rollback restores the
saved report file, never resets billing data.

The read phase uses a PostgreSQL REPEATABLE READ, READ ONLY transaction. Delivery
uses the existing BotNotification outbox with an advisory transaction lock and
original per-window dedupe key. Repeat execution/restart does not create another
notification for an already queued window. Long messages are split in one
transaction; recipients are admin-only. `--dry-run` does not enqueue anything.
An explicit one-time preview may use a separate fixed dedupe key. Confirm its
`sent_at` after the Telegram worker processes it before claiming delivery.

## Verification

`python -m pytest -q tests/test_admin_model_stats.py` exercises math, per-output
vs billed duration, zero/missing cost, refunds/holds, negative margin, duplicate
UUIDs, original FX, historical-rate warnings, boundaries, SQLite-backed real ORM
queries, and admin outbox dedupe. Production dry-run additionally exercises the
PostgreSQL query against real data without modifying finance or submitting any
paid provider requests.
