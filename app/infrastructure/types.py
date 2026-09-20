from decimal import Decimal
from uuid import uuid4

from sqlalchemy import DateTime, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column


def uuid_pk() -> Mapped[str]:
    return mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))


def money_column() -> Mapped[Decimal]:
    return mapped_column(Numeric(18, 2), nullable=False)


def utc_created_at() -> Mapped[object]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
