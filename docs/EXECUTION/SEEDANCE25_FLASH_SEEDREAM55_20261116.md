# ArgoLink: Seedance 2.5 Flash and Seedream 5.5 — scoped candidate (2026-11-16)

Status: **CANDIDATE**, not publicly enabled. Provider price-card review does
not constitute a paid/live smoke pass or authorization of partner RUB prices.

## Reviewed upstream contracts

Source: [ArgoLink catalog](https://argolink.io/api/catalog/v1/models?page_size=100)
revision `58436d31cd897732` (2026-11-16), with the individual model
pages [Seedance 2.5 Flash](https://argolink.io/en/models/seedance-2.5-flash)
and [Seedream 5.5](https://argolink.io/en/models/seedream-5.5).

| Model | Native route(s) | Procurement (USD) | Notes |
| --- | --- | --- | --- |
| `seedance-2.5-flash` | `POST /v1/videos/generations` | 480p: $0.0572/s; 720p: $0.129/s | 4–30s; 10 image, 5 video, 5 audio references; max 20 combined; one output |
| `seedance-2.5-flash` with input video | same | 480p: $0.0351/s; 720p: $0.0792/s | Input video seconds also bill; edit and extend supported; no 1080p or draft |
| `seedream-5.5` | `POST /v1/images/generations`, `POST /v1/images/edits` | 1K: $0.02/image; 2K: $0.035/image; plus **$0.008 per reference image** | PNG; 11 aspect ratios; 8 JSON/multipart references; one output; 2K default |

The Flash with-video-input tiers are obtained by applying the published
`0.6143` price multiplier to the supplier's tier prices, rounded to
four decimals for the existing procurement precision. Recheck any changed
supplier quote before setting partner retail prices. Do not substitute
discounted procurement for evidence of actual supplier settlement.

## Scope and economics

- Reviewed contracts are committed to `app/contracts/catalog.json`.
- Seedream 5.5 reference counts are included in the **admission reserve and final
  actual-only settlement**. JSON edits use the validated `images` array;
  multipart edits use the admitted file count stored with the generation.
  Invalid or failed images must not settle a charge.
- Flash video references select a distinct cost tier; existing reference-video
  billable-seconds and durable polling/reconciliation flows are reused.
- No existing model, historical generation, partner snapshot or ledger rate is
  rewritten. No global import is run.
- New models and provider capabilities are registered only through
  `python -m app.catalog.seed_models_2026 --apply` **after deployment**,
  inside a caller-owned DB transaction. This does not create partner prices,
  set the smoke flag or publish models. Default invocation shows a dry plan.

## Release gates

1. Exact SHA CI: Ruff, unit and route integration tests (successful image
   generation/edit, multiple references, failed edit, Flash video modes,
   native provider body, replay and idempotency).
2. Stage the candidate and register the two models in `draft`.
3. Determine *approved* partner prices in RUB for each capability, including
   Seedream reference fee and Flash video input mode. Use the existing
   pricing service to save them, with provider cost, FX and margin checks.
   Do not assume a constant FX rate or hardcode a markup.
4. Run at least one **paid** end-to-end supplier generation for each intended
   mode, plus status/result inspection, provider-cost reconciliation and
   safe replay. Confirm the Seedream returned PNG dimensions and reference
   charge and Flash actual billed seconds with a video reference.
5. Record actual smoke evidence. Set `has_successful_smoke=True` only for a
   model that was truly verified, then use the existing enable gate.
6. Verify `GET /v1/models`, public `/prices`, partner-specific pricing,
   docs, unavailable-model behavior, tenant isolation, and production logs
   for the deployed SHA before declaring a rollout successful.

## Rollback

Disable these two models using the existing admin mechanism. Do **not**
delete accepted generations or modify append-only billing history. Rolling
back the code alone is not sufficient if already-running generations refer
to the new model contracts; drain/reconcile them first.
