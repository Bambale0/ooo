from decimal import Decimal
from uuid import uuid4

from sqlalchemy import DateTime, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def uuid_pk() -> Mapped[str]:
    return mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))


def money_column() -> Mapped[Decimal]:
    return mapped_column(Numeric(18, 2), nullable=False)


def utc_created_at() -> Mapped[object]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ExactNumeric(TypeDecorator):
    """PostgreSQL NUMERIC; exact string round-trips in isolated SQLite tests."""

    impl = Numeric
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(80))
        return dialect.type_descriptor(Numeric(self.impl.precision, self.impl.scale))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return str(value) if dialect.name == "sqlite" else Decimal(value)

    def process_result_value(self, value, dialect):
        return Decimal(str(value)) if value is not None else None
