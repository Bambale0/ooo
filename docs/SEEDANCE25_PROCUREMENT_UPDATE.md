# Reviewed Seedance 2.5 procurement update

ArgoLink's supplier notice changes the procurement rates effective 2026-10-06T12:17:00Z to USD 0.0874 / 0.196 / 0.483 per output second for 480p / 720p / 1080p. These user-supplied rates are also present in the supplier's live model catalog. Only ordinary Seedance 2.5 is updated; other models and partner prices are unchanged.

The reviewed update is stored in `app/contracts/catalog.json`, including its source notice, expected previous rates, effective timestamp and supplier-reported compensation. The overall catalog revision is not presented as a fresh review of every model.

## Apply after review and CI

```bash
python -m alembic upgrade head
python -m ops.seedance25_procurement
python -m ops.seedance25_procurement --apply
```

The default dry-run rolls back its transaction. Apply first rechecks the live supplier tiers; any mismatch stops before database writes. It updates procurement rows and records price history without changing partner prices or accepted generation snapshots. The supplier compensation statement and safe historical attempt enrichment are committed in the same transaction. Repeating the command does not duplicate price history or compensation. Unexpected existing procurement rates require a fresh review.

## Accounting semantics

- Provider-reported money is parsed exactly and recorded per attempt. A malformed reported debit requires reconciliation.
- A cost computed from billed seconds and an accepted procurement rate is marked `estimated`, not confirmed supplier expenditure.
- Admin attempt reconciliation replaces an estimate with a confirmed charge through an append-only coverage adjustment. It preserves accepted retail rates, partner charges and other unresolved cost holds. Repeated confirmation is idempotent.
- Historical enrichment copies an existing generation amount into its single successful ArgoLink attempt and marks it estimated; generation amounts are not recomputed. Histories involving multiple providers need explicit attribution.
- The separately recorded supplier credit is `supplier_reported`, with a unique provider/reference. It never changes partner credit or withdrawable cash. Independent supplier credit/debit history is still needed to prove the credit and determine net procurement expenditure.
- InfAI retains the existing group-specific token billing and equivalent-request checks. Its per-task actual cost uses returned completion tokens and the pinned rate. A generic model base price from a provider catalog is not a substitute for its video-token tariff.

## Verify after apply

Check the three procurement tiers, unchanged partner prices and accepted generation snapshots, exactly one supplier credit statement, and per-attempt estimated/reported costs. Run the existing financial monitoring and reconcile unknown attempts against supplier records. Paid primary failures plus a fallback may create an incident loss; that loss does not increase the user's accepted price.

No runtime change is performed merely by creating this PR. The data update requires the explicit apply command after the migration. Review rollback data before downgrading: downgrade removes the provider-credit table.

Project methods used: [test-driven-development](https://github.com/Bambale0/.agents/blob/e4a2e491647fb9d88a61e50f1f05f5059235f09f/skills/test-driven-development/SKILL.md) and [verification-before-completion](https://github.com/Bambale0/.agents/blob/e4a2e491647fb9d88a61e50f1f05f5059235f09f/skills/verification-before-completion/SKILL.md).
