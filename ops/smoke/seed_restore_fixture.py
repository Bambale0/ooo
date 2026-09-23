#!/usr/bin/env python3
"""Populate an isolated migrated database before a real disaster-recovery drill."""

import argparse
import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from app.accounts.models import ApiKey, Partner
from app.billing.service import apply_cost_coverage_change, apply_partner_balance_change
from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.infrastructure.database import SessionLocal, engine
from app.infrastructure.security import hash_secret
from app.payments.models import PaymentInvoice
from app.support.models import SupportAttachment, SupportMessage, SupportTicket


async def seed(root: Path):
    if get_settings().app_env == "production":
        raise RuntimeError("Restore fixtures are forbidden in production")
    marker = str(uuid4())
    async with SessionLocal() as db:
        partner = Partner(telegram_id=f"restore-{marker}", company_name="Restore fixture", project_name="DR")
        db.add(partner)
        await db.flush()
        db.add(ApiKey(partner_id=partner.id, name="DR fixture", key_hash=hash_secret(marker), key_prefix="fixture"))
        payment = PaymentInvoice(
            partner_id=partner.id,
            idempotency_key=marker,
            requested_rub=Decimal("1500"),
            status="credited",
            paid_at=datetime.now(UTC),
            credited_at=datetime.now(UTC),
            paid_asset="USDT",
            paid_amount=Decimal("15"),
            paid_usd_rate=Decimal("1"),
        )
        db.add(payment)
        generation = Generation(
            partner_id=partner.id,
            model_id=str(uuid4()),
            model_slug="restore-fixture",
            mode="default",
            resolution="720p",
            idempotency_key=marker,
            status="completed",
            partner_price_rub=Decimal("1"),
            provider_cost_usdt_snapshot=Decimal(".01"),
            rub_per_usdt_snapshot=Decimal("100"),
            provider_cost_reserve_rub=Decimal("1"),
            prompt="synthetic DR fixture",
        )
        db.add(generation)
        await db.flush()
        for amount, operation in [(Decimal("1500"), "payment_credit"), (Decimal("-1"), "generation_reserve")]:
            await apply_partner_balance_change(db, partner, amount, operation, f"dr-retail:{marker}:{operation}")
            await apply_cost_coverage_change(db, partner, amount, operation, f"dr-coverage:{marker}:{operation}")
        ticket = SupportTicket(partner_id=partner.id, subject="Restore attachment")
        db.add(ticket)
        await db.flush()
        message = SupportMessage(
            ticket_id=ticket.id,
            sender_type="partner",
            sender_telegram_id=partner.telegram_id,
            text="Synthetic attachment, no customer data",
        )
        db.add(message)
        await db.flush()
        path = root / f"{marker}.bin"
        await asyncio.to_thread(path.write_bytes, b"restore-fixture")
        db.add(
            SupportAttachment(
                message_id=message.id, file_name="fixture.txt", storage_path=str(path), file_size_bytes=15
            )
        )
        await db.commit()
    await engine.dispose()
    print("restore_fixture_created: partner, payment, generation, API key, ledgers, support attachment")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--support-dir", type=Path, required=True)
    parser.add_argument("--confirm-isolated", action="store_true", required=True)
    arguments = parser.parse_args()
    arguments.support_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(seed(arguments.support_dir.resolve()))
