# Seedream 5.0 Pro - scoped integration (2026-10-19)

Source: https://argolink.io/en/models/seedream-5.0-pro and live public catalog,
re-reviewed 2026-10-31 at catalog revision `5cf5790b965e2bad`.
ID: `seedream-5.0-pro`; generation and JSON/multipart edits; one returned JPEG;
1K or 2K (2K by default), eleven reviewed aspect ratios; eight reference images,
48 MiB combined. HTTPS-reference content size is enforced upstream.
Procurement is the reviewed catalog's USD 0.03 per returned image for either tier.
No changes to existing model pricing, account balances or old generations.

## Verification

Behavioral tests cover exact default tier, forwarding, billing once, replay,
reference count/size, unsupported controls, malformed results, URL validation,
and upstream refusal without a customer debit. Existing model behavior is retained.
Four initial behavioral checks failed on the pre-integration revision.

A real paid supplier generation attempt was blocked by the tool safety boundary
before execution in this session. No real generated output or paid smoke success
has been observed. Do not mark `has_successful_smoke` true or enable this model
for sale on that basis. Keep it draft until the existing launch gate is satisfied.

## Registration

Use existing model and price services, in a single transaction, for this ID only.
Create a draft image model, integration/docs flags true after code deployment,
smoke flag false until verified. Create the two capabilities for ArgoLink.
For each tier, the owner-approved retail rule is current procurement converted by
the production internal RUB/USDT valuation plus RUB 1.30 per returned image. Use
`upsert_price` so non-custom partner snapshots receive the published rate and
custom partner rates remain untouched. Re-running registration must not modify
unrelated models or historic generation snapshots.
Never run a global catalog import: unrelated upstream changes need their own review.

## Remaining launch gate

Verify actual generation and editing, then set the truthful smoke flag and use
`enable_model`, which rechecks economic and contract gates. Verify public catalog,
pricing, installed native route, replay, and unchanged unrelated price snapshots.
