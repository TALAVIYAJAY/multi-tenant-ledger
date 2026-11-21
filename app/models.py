import enum
from datetime import datetime
from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.db import Base


class TransactionType(str, enum.Enum):
    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    FEE = "FEE"
    REVERSAL = "REVERSAL"


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    firm_id = Column(String(64), nullable=False, index=True)
    client_ref = Column(String(64), nullable=False, index=True)
    amount = Column(Numeric(18, 4), nullable=False)
    type = Column(Enum(TransactionType), nullable=False)
    currency = Column(String(3), nullable=False, default="USD")
    effective_date = Column(DateTime, nullable=False, index=True)
    idempotency_key = Column(String(128), nullable=True, index=True)
    reversal_of_id = Column(Integer, ForeignKey("ledger_entries.id"), nullable=True)
    description = Column(String(255), nullable=True)
    created_by = Column(String(64), nullable=False, default="system")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Relationships
    reversal_entry = relationship("LedgerEntry", remote_side=[id], backref="reversed_by")

    __table_args__ = (
        Index("ix_firm_client_date", "firm_id", "client_ref", "effective_date"),
        UniqueConstraint("firm_id", "idempotency_key", name="uq_firm_idempotency"),
    )
