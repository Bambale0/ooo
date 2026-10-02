# Partner-Specific Pricing Deployment Guide

## Overview

This deployment implements per-partner pricing with:
- 35% gross margin for **new partners** (price = cost_rub / 0.65)
- Frozen current rates for **existing partners** via snapshot

## Pre-Deployment Checklist

- [ ] PR #73 reviewed and approved
- [ ] CI checks passing
- [ ] Backup production database
- [ ] Verify current FX rate is reasonable for margin calculations

## Deployment Steps

### 1. Run Database Migration

```bash
# On production server
cd /path/to/ooo
source .venv/bin/activate
alembic upgrade head
```

**Migration**: `20261002_0023_partner_specific_pricing.py`

**Changes**:
- Adds `partner_id` column to `partner_prices` (nullable, indexed, FK to partners)
- Adds `partner_id` column to `partner_price_history` (nullable, indexed)
- Updates unique constraint to include `partner_id`

**Rollback**: `alembic downgrade -1` (removes columns and restores original constraint)

### 2. Restart Application

```bash
# Example with systemd
sudo systemctl restart ooo-api
sudo systemctl restart ooo-workers
```

### 3. Verify Migration

```bash
# Check that new columns exist
psql -d ooo_production -c "\d partner_prices"
# Should show partner_id column with type VARCHAR(36) NULLABLE

# Verify global prices still exist (partner_id IS NULL)
psql -d ooo_production -c "SELECT COUNT(*) FROM partner_prices WHERE partner_id IS NULL;"
```

### 4. Snapshot Existing Partners

For each **existing partner** (those who should keep current rates):

```bash
curl -X POST https://api.ooo.production/pricing/snapshot/{partner_id} \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json"
```

**Response**:
```json
{
  "partner_id": "uuid-here",
  "prices_created": 42
}
```

**Script for bulk snapshot**:
```bash
#!/bin/bash
# Get all active partner IDs
psql -d ooo_production -t -c "SELECT id FROM partners WHERE status = 'active';" | while read partner_id; do
  if [ ! -z "$partner_id" ]; then
    echo "Snapshotting partner: $partner_id"
    curl -X POST "https://api.ooo.production/pricing/snapshot/$partner_id" \
      -H "Authorization: Bearer $ADMIN_TOKEN" \
      -H "Content-Type: application/json"
    sleep 0.5
  fi
done
```

### 5. Create Pricing for New Partners

For **new partners** (going forward), use the 35% margin endpoint:

```bash
curl -X POST https://api.ooo.production/pricing/new-partner/{partner_id} \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json"
```

**Response**:
```json
{
  "partner_id": "uuid-here",
  "prices_created": 42,
  "margin_percent": 35
}
```

## Verification Tests

### Test 1: Price Resolution for Existing Partner

```bash
# Should use partner-specific price (snapshotted value)
# Make a generation request as existing partner
# Check Generation.partner_price_rub matches snapshotted price, not current global
```

### Test 2: Price Resolution for New Partner

```bash
# Should use new partner price (35% margin)
# Make a generation request as new partner
# Verify partner_price_rub = (provider_cost_usdt * fx_rate) / 0.65
```

### Test 3: Global Price Change Doesn't Affect Partners

```bash
# Update a global price
curl -X PUT https://api.ooo.production/pricing \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{"model_slug":"test-model","mode":"default","resolution":"default","price_rub":"100.00","provider_cost_usdt":"50.0","billing_unit":"generation"}'

# Verify partner-specific prices unchanged
psql -d ooo_production -c "SELECT partner_id, price_rub FROM partner_prices WHERE model_id = (SELECT id FROM models WHERE slug = 'test-model') AND partner_id IS NOT NULL;"
```

### Test 4: Admin Endpoints Work

```bash
# List global prices
curl https://api.ooo.production/pricing

# List partner-specific prices
curl "https://api.ooo.production/pricing?partner_id={partner_id}"
```

## Monitoring

After deployment, monitor:

1. **Margin incidents**: Check `/billing/incidents` for any new negative-margin alerts
2. **Generation success rate**: Verify partners can still create generations
3. **Price resolution**: Sample a few generations, verify `partner_price_rub` matches expectations
4. **Database performance**: Check query performance on `partner_prices` with new index

## Rollback Plan

If issues arise:

1. **Stop accepting new work** (disable partner API access temporarily)
2. **Run migration downgrade**:
   ```bash
   alembic downgrade -1
   ```
3. **Restart application**
4. **Verify global prices restored**

**Note**: Rolling back will lose all partner-specific prices created after deployment. Existing generations keep their snapshotted prices in `Generation.partner_price_rub`.

## Post-Deployment Tasks

- [ ] Document partner pricing policy in operator handbook
- [ ] Update partner onboarding workflow to auto-create 35% margin pricing
- [ ] Add Grafana dashboard for partner-specific pricing metrics
- [ ] Schedule review of margin percentages after 1 month

## Support Contacts

- Database issues: DBA team
- API issues: Backend team
- Business logic: Product team
- Financial concerns: Finance/accounting team

---

**Deployed by**: _______________________  
**Date**: _______________________  
**Production SHA**: 8e69190 (feat: partner-specific pricing with 35% margin)
