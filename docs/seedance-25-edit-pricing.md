# Seedance 2.5: edit-only cost-plus pricing

Confirmed product decision, 2026-10-09: apply procurement cost in RUB plus 2.50 RUB **per billed second**, only when `model=seedance-2.5` and `omni_reference_task_type=edit`. Ordinary `reference`, `auto`, text/image generation and other model IDs keep their existing rates. Existing generations are never repriced.

## Calculation

`rate_rub = edit_provider_cost_usdt_per_second * accepted_rub_per_usdt + configured_markup_rub_per_second`

`final_charge_rub = ROUND_HALF_UP(rate_rub * provider_usage.billed_seconds, 0.01)`

Billed seconds include the output and chargeable input video. Editing a 10-second source into a 10-second output normally bills 20 seconds. This is not a flat 2.50 RUB surcharge per request and not a surcharge only on the output duration.

The existing conservative edit reserve remains 60 billed seconds. On success, the original accepted rate is multiplied by actual provider-reported `billed_seconds` and the unused reserve is returned once. A definitive failure releases the customer reserve through the existing idempotent ledger service. An uncertain submit continues to follow the existing reconciliation policy; this change does not claim that those provider outcomes are known.

Full Decimal precision is retained at the unit-rate stage. Only the final RUB charge is rounded. Never multiply a two-decimal display approximation to reconstruct a bill.

## Configuration and sources

- `SEEDANCE_25_EDIT_MARKUP_RUB_PER_SECOND` is a nonnegative finite Decimal in the existing Pydantic Settings configuration. Omitted means disabled; deploy code without changing billing first.
- Procurement is read from the model's `PartnerPrice` rows with `mode=edit`, the requested resolution and `billing_unit=second`. Missing edit procurement fails before reserving money; no silent fallback to `default`.
- FX comes from the existing `current_fx` source. No additional exchange-rate polling is introduced.
- The accepted FX, cost rate and computed retail rate are saved in the existing immutable generation snapshot. Changing configuration, FX or catalogue rates later does not affect settlement or idempotent retries of that request.
- The stored `PartnerPrice.price_rub` for edit is the publication-time display baseline; enabled cost-plus quotes and public catalogue prices recompute from procurement and current FX. `default` rows and their partner snapshots remain untouched.
- `GET /api/v1/catalog/pricing` exposes separate `mode=edit` retail rows when enabled, without exposing procurement. Default rows remain unchanged.

ArgoLink's published Seedance 2.5 rates checked 2026-10-09: 480p USD 0.0874/s, 720p USD 0.196/s, 1080p USD 0.483/s. These are deployment inputs, not constants in billing code. Verify the supplier before later changes: https://argolink.io/en/models/seedance-2.5 .

## Activation and rollback

1. Merge only after regression and full CI pass; use the normal immutable-image deployment.
2. With the setting still omitted, create only the three `mode=edit` procurement/price rows through the existing pricing service. Snapshot new variants for existing non-deleted partners. Do not import or republish unrelated default prices.
3. Persist `SEEDANCE_25_EDIT_MARKUP_RUB_PER_SECOND=2.50` in production configuration and recreate the runtime services on the same verified image.
4. Verify the public edit prices, all partner effective edit quotes, unchanged default/reference rates, and read-only historical financial snapshots.
5. For policy rollback, remove the setting and restart the same services. New edits use the previous default policy. Never edit historical rates, balances or ledger entries to roll back a policy.

Focused regression suite: `pytest tests/test_seedance_edit_pricing.py`. It covers every resolution, URL aliases, reference/auto isolation, missing procurement, configuration validation, immutable acceptance, repeated POST/poll/settlement, definitive failure release and public catalogue output.
