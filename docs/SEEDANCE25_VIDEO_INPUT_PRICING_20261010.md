# Seedance 2.5 video-input pricing

Approved 2026-10-10: every new Seedance 2.5 request containing at least one
reference_videos entry receives the existing edit retail price for its
resolution. Applies equally to provider edit and reference with additional
images/audio. Pure image/audio/text/frames use the ordinary default retail price.

This changes price selection only; do not rewrite the provider request mode,
reference assets, duration or aspect ratio.

Pricing is based on billable seconds reported by the supplier: output duration
plus billable reference-video duration. For example, 11 seconds output + 10
seconds reference = 21 billable seconds. Pre-submission reserve remains
conservative because input duration is not trusted to the client. Final
settlement releases excess reserve and charges on frozen retail rates.

The provider's documented video-input procurement rates (USD/billable second)
are 480p 0.0523, 720p 0.117, 1080p 0.289; these are estimates in the
generation snapshot unless the supplier reports an attributable actual debit.
The provider may charge more precision than the rounded rate card (example:
2.461368 USD / 21 seconds vs 0.117 USD on the card). Do not mistake estimates
for confirmed supplier transactions; reconcile separately and append
corrections without rewriting original ledgers.

The existing edit retail mode uses the deployed optional markup policy:
retail rate = stored edit procurement price x current FX + configured RUB
markup, not necessarily the static price_rub in the database. API /pricing
and website /price must display the same active rate. Historic already-accepted
generation snapshots and ledger entries are never retroactively repriced;
existing partner price snapshots remain protected.

Release checklist: test video+image+audio, plain edit, no video, all tiers,
provider request preservation, positive economy, actual usage, idempotency,
legacy snapshots, public price, and no new paid smoke generation.
