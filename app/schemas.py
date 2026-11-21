from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import TransactionType

ALLOWED_CURRENCIES = {"USD", "EUR", "GBP", "CAD", "AUD", "CHF", "JPY"}


class TransactionCreate(BaseModel):
    client_ref: str = Field(..., min_length=1, description="Opaque client reference")
    amount: Decimal = Field(..., gt=0, description="Transaction amount (must be positive)")
    type: TransactionType = Field(..., description="DEPOSIT, WITHDRAWAL, FEE, REVERSAL")
    currency: str = Field(default="USD", min_length=3, max_length=3)
    effective_date: datetime = Field(..., description="Date transaction occurred")
    idempotency_key: Optional[str] = Field(default=None, max_length=128)
    description: Optional[str] = Field(default=None, max_length=255)
    created_by: str = Field(default="system", max_length=64)

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        upper_v = v.upper()
        if upper_v not in ALLOWED_CURRENCIES:
            raise ValueError(f"Unsupported currency: {v}. Must be one of {sorted(ALLOWED_CURRENCIES)}")
        return upper_v

    @field_validator("effective_date")
    @classmethod
    def validate_date(cls, v: datetime) -> datetime:
        # Effective date cannot be unreasonably far in future (e.g. max 1 day ahead for timezone slack)
        now = datetime.utcnow()
        if (v - now).total_seconds() > 86400:
            raise ValueError("Effective date cannot be in the future")
        return v


class RowError(BaseModel):
    row_index: int
    client_ref: Optional[str] = None
    error: str


class BatchIngestRequest(BaseModel):
    transactions: List[Dict[str, Any]] = Field(
        ...,
        description="Raw transaction rows to allow partial success on dirty data",
    )


class BatchIngestResponse(BaseModel):
    total_rows: int
    accepted_rows: int
    failed_rows: int
    duplicate_rows: int
    errors: List[RowError]


class BalanceResponse(BaseModel):
    firm_id: str
    client_ref: str
    currency: str
    as_of: datetime
    balance: Decimal
    transaction_count: int


class PeriodSummaryResponse(BaseModel):
    firm_id: str
    client_ref: str
    currency: str
    from_date: datetime
    to_date: datetime
    total_inflows: Decimal
    total_outflows: Decimal
    total_fees: Decimal
    net_change: Decimal


class ReversalRequest(BaseModel):
    reason: str = Field(..., min_length=3, max_length=255, description="Compliance audit reason for reversal")
    created_by: str = Field(..., min_length=1, max_length=64, description="Staff identifier performing the reversal")


class LedgerEntryOut(BaseModel):
    id: int
    firm_id: str
    client_ref: str
    amount: Decimal
    type: TransactionType
    currency: str
    effective_date: datetime
    idempotency_key: Optional[str]
    reversal_of_id: Optional[int]
    description: Optional[str]
    created_by: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
