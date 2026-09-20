from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database import Base
from app.infrastructure.types import utc_created_at, uuid_pk


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_ledger_entries_idempotency_key"),)

    id: Mapped[str] = uuid_pk()
    partner_id: Mapped[str] = mapped_column(ForeignKey("partners.id"), nullable=False, index=True)
    operation_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    amount_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    balance_after_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    generation_id: Mapped[str | None] = mapped_column(String(36), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[object] = utc_created_at()
