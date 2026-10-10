# Pricing Operations

## Purpose

Global partner prices are published atomically for every partner and apply to new generations.

- `partner_prices.price_rub` is the retail template for future partners and newly introduced variants.
- `partner_price_snapshots.price_rub` is the partner's live retail price used at admission; `is_custom=true` marks a durable, partner-specific override immune to global publication.
- `partner_prices.provider_cost_usdt` remains live procurement cost for every partner.
- Generation admission compares the frozen partner retail price against the current procurement cost and current FX rate.

Every generation stores its accepted price, so in-flight and historical operations remain immutable after publication.

## Deployment sequence

1. Deploy code and run Alembic through `20261002_0025`.
2. Do not change global retail templates before snapshot verification is complete.
3. Verify snapshot coverage:
   - active/non-deleted partner count;
   - global pricing variant count;
   - expected snapshot count is partner count x pricing variant count;
   - query for missing partner/price pairs must return zero rows.
4. Run the financial incident tick once after migrations.
5. Check `FinancialIncident` for:
   - `partner-margin:<partner_id>:<price_id>`;
   - `partner-economics:<partner_id>:<price_id>`.
6. Any `partner-economics` incident is a negative-margin configuration. Do not enable a more expensive fallback or raise procurement ceilings for that partner until the retail price or procurement economics are corrected.
7. Only after the above checks may global template retail prices be changed for future partners.

## Snapshot behavior

### Existing partners during migration

Migration `20261002_0025` atomically inserts one retail snapshot for every non-deleted partner and every existing `partner_prices` row.

Ordinary snapshots follow a confirmed global publication. Custom snapshots change only through an authenticated partner-specific update, never through global publication.

### New partner approval

Partner approval calls `snapshot_partner_prices()`. The new partner receives the current retail template for every existing pricing variant.

Later global publications update non-custom snapshots; custom snapshots retain their negotiated rate.

### New pricing variant

When a brand-new `model/mode/resolution` pricing variant is created through the admin pricing endpoint, its initial retail price is snapshotted for all existing non-deleted partners.

Later confirmed updates of that variant change the template and all non-custom snapshots atomically. Custom snapshots are excluded.

A safe lazy enrollment exists only for variants whose `created_at` is newer than the partner. A missing snapshot for a variant older than the partner is treated as a rollout/data-integrity problem and pricing stays fail-closed.

## Creating prices for a new partner

1. Configure or review global `partner_prices` first.
2. Verify current procurement cost and FX economics. Retail must cover procurement at the current FX rate.
3. Approve the partner only after the intended global retail templates are correct.
4. Approval snapshots those retail prices for the partner.
5. Verify the partner has the expected snapshot count before issuing production traffic.
6. A negotiated partner rate is set via admin-only `PUT /api/v1/catalog/pricing/partners/{partner_id}` with `model_slug`, `mode`, `resolution`, `price_rub` (RUB per configured billing unit), and a nonempty `reason`. It is scoped to that partner and variant, recorded in an append-only history, and does not modify past generations.
7. Run the financial incident tick and verify no `partner-economics` incident exists for the partner.

## Changing global prices

A global retail update applies to all new generation requests after publication.

Before changing it:

- inspect current partner snapshots;
- check current procurement and FX;
- record the commercial reason;
- prepare the exact old/new diff for every affected variant.

After changing it:

- verify every ordinary (`is_custom=false`) snapshot changed to the published value and every custom (`is_custom=true`) snapshot stayed unchanged;
- verify in-flight and historical generation snapshots did not change;
- run the financial incident tick.

## Margin monitoring

For each partner snapshot, margin is evaluated using:

`frozen_partner_retail_rub - current_provider_cost_usdt * current_rub_per_usdt`

The threshold hierarchy remains configuration -> model -> global.

Two incident families are emitted:

- `partner-margin`: positive but below the configured margin threshold;
- `partner-economics`: negative margin.

The global template incidents remain in place as a guard for prices offered to future partners.

## Post-deploy verification queries

Run against production PostgreSQL after migration:

```sql
SELECT count(*) AS partners
FROM partners
WHERE status <> 'deleted';

SELECT count(*) AS pricing_variants
FROM partner_prices;

SELECT count(*) AS snapshots
FROM partner_price_snapshots;

SELECT p.id AS partner_id, pp.id AS partner_price_id
FROM partners p
CROSS JOIN partner_prices pp
LEFT JOIN partner_price_snapshots s
  ON s.partner_id = p.id
 AND s.partner_price_id = pp.id
WHERE p.status <> 'deleted'
  AND s.partner_id IS NULL;
```

The last query must return zero rows before global retail prices are changed.

To inspect negative-margin partner snapshots with an explicit FX rate `:rub_per_usdt`:

```sql
SELECT
    p.id AS partner_id,
    p.project_name,
    m.slug,
    pp.mode,
    pp.resolution,
    s.price_rub AS frozen_retail_rub,
    pp.provider_cost_usdt,
    pp.provider_cost_usdt * :rub_per_usdt AS procurement_rub
FROM partner_price_snapshots s
JOIN partners p ON p.id = s.partner_id
JOIN partner_prices pp ON pp.id = s.partner_price_id
JOIN models m ON m.id = pp.model_id
WHERE p.status <> 'deleted'
  AND s.price_rub < pp.provider_cost_usdt * :rub_per_usdt
ORDER BY p.project_name, m.slug, pp.mode, pp.resolution;
```

Any returned row is a rollout blocker for pricing/fallback changes affecting that configuration.


## Preserving negotiated partner rates

Migration `20261010_0028` adds an explicit `is_custom` marker and append-only
`partner_price_override_history` audit. Any existing snapshot whose price differs
from its global template is marked custom, without changing either price or
any historical generation, ledger, refund, or balance. Migration import audit
entries are timestamped when imported, not backdated to the original agreement.

Admin-only per-partner writes validate the variant, partner, decimal precision,
and current procurement/FX floor. Updating the same rate twice is idempotent.
Changes to the global template skip custom snapshots; a custom rate stays fixed
even if the global price momentarily equals it. Global procurement updates that
would make a custom rate loss-making are rejected (HTTP 409). Rollback to old
application code is unsafe during subsequent global price edits; a schema
downgrade is blocked while custom rows exist. Check the live revision and
schema before applying a global pricing update.
