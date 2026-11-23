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


## 2026-11-23: off-size Seedream output recovery (KSU delivery)

Production correlation: KSU `7a10f069-784f-4c07-97f9-046070256708`
→ Neironych `22ae7708-854d-46df-8211-65af0b50d90f`
→ Argolink `images/edits`. The source returned HTTP 200 and a decoded
848×1104 raster for a documented 1K 3:4 request expecting 960×1280.
An independent KSU request, `d31f2de1-b8fd-4592-8704-4c9097b3a4b6`,
showed the same actual/expected dimensions. The existing
`ImageResultDimensionsMismatch` was correct for the *billable exact-size*
contract; however, the existing safe recovery predicate required less than
1% aspect drift, while this image differs by ~2.36%. The gateway returned
503 `provider_response_invalid`, and KSU eventually failed/refunded the
ambiguous synchronous submission without receiving its image.

The focused repair expands **only** the already existing Seedream-specific,
unbilled near-frame recovery envelope from 1% to 3%. The strict dimension
check in `image_usage` still fails mismatched rasters, and the router's
`preserve_unbilled_result` path delivers only fully decoded, profile-format
validated, bounded output; it leaves provider usage/cost reconciliation pending,
rather than charging the partner as if the exact requested raster arrived.
Other models, malformed/truncated media, markedly wrong framing, very small
images, oversized rasters, pricing, idempotency and partner routing are unchanged.

Production database audit of the KSU partner's last seven days showed 12
`image_result_invalid_dimensions`, eight policy refusals, two invalid
image counts, one invalid format and ten native completions. The other
failure types are **not** treated as recoverable image deliveries.

Existing historical failed jobs contain no native response asset in the
Neironych database; no replay, refund reversal, provider POST or manual
media delivery is authorized by this fix. Historical restoration would
require a separate safe read-only recovery source and customer-account
reconciliation. Release must pass exact-head CI and a smoke that verifies
the updated deployed predicate without any paid generation.
