# ArgoLink terminal failure and partner charge presentation

Decision 2026-10-10: do not present a technical reservation as a final debit
in partner-facing histories or in administrator generation summaries. The
full append-only financial ledger remains available only to administrators.

Provider evidence:
https://argolink.io/en/docs : failed or expired generation jobs are not charged;
billing occurs only after a delivered video is completed. A definitive ArgoLink
video task status of failed with no contradictory provider-reported cost is
therefore a documented confirmed-free provider attempt (cost USD 0.00), not
an unknown procurement obligation. Zero applies to THIS attempt only, not
earlier/later attempts from other providers. A provider-supplied explicit
charge takes precedence pending manual review. Uncertain POST status, polling
errors, timeouts, or reconciliation-required results are not proof of failure.

Partner's full reservation remains reversible and invisible from operation
history. For terminal failed tasks, show zero actual charge, not the originally
quoted maximum. For in-flight/unknown tasks, show "no final charge yet".
Legacy completed tasks use the existing settled ledger query.

Pre-admission reservation remains an upper-bound for unverified reference
video durations, not a claim about actual billable seconds. The user requested
a media-measured input reservation; implementing that safely requires a
dedicated SSRF-safe and duration-authenticating preflight that does not trust
client-provided length, supports multiple/remote clips and never underfunds
a provider charge. The pricing and final settlement STILL use the actual
provider billed_seconds. Do not silently reduce the initial hold based only
on a client-provided value. This is an outstanding scope item.

Already-accepted generation/ledger snapshots must not be silently overwritten;
historical confirmed Argo failures may be reconciled through the audited
admin attempt-cost reconciliation path using outcome free and source reason.
