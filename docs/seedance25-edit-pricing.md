# Seedance 2.5 edit pricing

Decision: 2026-10-09. Scope is **only** `model=seedance-2.5` with
`omni_reference_task_type=edit`. Ordinary `reference` (including a video), `auto`,
text/frame generation, other models and previously accepted requests keep their
existing tariffs.

## Request

Use `POST /v1/videos/generations` with the usual partner Bearer key and a durable
`Idempotency-Key`:

```json
{
  "model": "seedance-2.5",
  "omni_reference_task_type": "edit",
  "prompt": "Make the colors warmer and keep everything else",
  "resolution": "720p",
  "reference_videos": [{"url": "https://example.org/street.mp4"}]
}
```

Exactly one HTTPS video, 4-30 seconds, is the editing target. Do not attach image
or audio references or frame inputs. Leave duration and aspect ratio out: the
output inherits them. Existing `duration=-1` / `aspect_ratio=adaptive` sentinels
remain accepted for compatibility; fixed duration/ratio and size are rejected.
The provider validates actual media contents/duration.

## Billing

Set `SEEDANCE_25_EDIT_MARKUP_RUB_PER_SECOND=2.50` in server configuration.
Omitted configuration disables this rule and preserves the previous fixed-price
behavior. Markup is a finite nonnegative Decimal, not a binary float.

Create separate `partner_prices` rows with `mode=edit`, `billing_unit=second`,
and each supported resolution. Their `provider_cost_usdt` is the reviewed
procurement rate. Never reuse or change the `default` rows for this rollout.
`price_rub` on edit templates/snapshots is an administrative reference value;
when enabled, admission and the public catalog derive the authoritative rate:

```
rate_rub_per_second = edit_procurement_usdt_per_second * accepted_fx + configured_markup
charge_rub = ROUND_HALF_UP(rate_rub_per_second * provider_billed_seconds, 0.01)
```

No intermediate rounding of the per-second rate. Procurement, FX, resulting rate
and markup are captured in the accepted generation snapshot. Future FX, cost or
markup changes cannot reprice that generation, including duplicate submissions,
late success or repeated settlement.

`billed_seconds` includes output plus the input video. Editing a 10-second video
normally bills 20 seconds, hence the markup is 50 RUB, not 25 RUB or a single
2.50 RUB fee. Do not confuse billable seconds with the finished video's duration.

Admission retains the existing conservative bound of 60 billable seconds for
edit (30 input + 30 output). Successful settlement releases the difference.
Failures and uncertain submissions retain the existing refund/reconciliation
policy; this change does not rewrite historical balances or fix old incidents.

`GET /api/v1/catalog/pricing` publishes a separate `mode=edit` current rate only
when the rule is enabled. `mode=default` remains the price for all other requests.
An enabled edit policy with a missing procurement row fails closed; it does not
silently fall back to the default retail price.

## Deployment

1. Verify tests and deploy the immutable code revision with the setting absent.
2. Review current provider rates. Create all three edit rows through the existing
   pricing service, including price history and snapshots for current partners.
3. Back up the current environment configuration, set the markup and recreate
   the runtime containers using the same verified image/revision.
4. Verify public prices, edit/non-edit admission quotes, exact settlement and
   absence of changes to existing generation snapshots and default tariff rows.
5. Rollback: remove only the markup setting and recreate the same services. Keep
   accepted snapshots and ledger entries. Do not delete historical price records.

Provider contract reviewed at https://argolink.io/en/models/seedance-2.5.
