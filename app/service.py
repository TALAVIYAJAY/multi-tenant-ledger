from datetime import datetime
from decimal import Decimal
from typing import List, Optional
from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.models import LedgerEntry, TransactionType
from app.schemas import (
    BalanceResponse,
    BatchIngestRequest,
    BatchIngestResponse,
    PeriodSummaryResponse,
    ReversalRequest,
    RowError,
    TransactionCreate,
)


def process_batch_ingest(
    db: Session,
    firm_id: str,
    batch: BatchIngestRequest,
) -> BatchIngestResponse:
    """
    Processes a bulk transaction ingestion batch.
    Applies row-level validation (partial success) and deduplication via idempotency keys.
    """
    errors: List[RowError] = []
    to_insert: List[LedgerEntry] = []
    seen_batch_keys = set()
    duplicate_rows = 0

    # Fetch existing idempotency keys for this firm upfront to minimize queries
    incoming_keys = [
        row.get("idempotency_key")
        for row in batch.transactions
        if row.get("idempotency_key")
    ]
    existing_keys = set()
    if incoming_keys:
        existing_records = (
            db.query(LedgerEntry.idempotency_key)
            .filter(
                LedgerEntry.firm_id == firm_id,
                LedgerEntry.idempotency_key.in_(incoming_keys),
            )
            .all()
        )
        existing_keys = {k[0] for k in existing_records}

    for idx, raw_row in enumerate(batch.transactions, start=1):
        client_ref = raw_row.get("client_ref")
        try:
            validated = TransactionCreate.model_validate(raw_row)
        except ValidationError as e:
            # Extract first error message for clean reporting
            msg = e.errors()[0]["msg"] if e.errors() else "Invalid row data"
            errors.append(RowError(row_index=idx, client_ref=client_ref, error=msg))
            continue
        except Exception as e:
            errors.append(RowError(row_index=idx, client_ref=client_ref, error=str(e)))
            continue

        # Duplicate detection (idempotency key)
        if validated.idempotency_key:
            if (
                validated.idempotency_key in existing_keys
                or validated.idempotency_key in seen_batch_keys
            ):
                duplicate_rows += 1
                continue
            seen_batch_keys.add(validated.idempotency_key)

        entry = LedgerEntry(
            firm_id=firm_id,
            client_ref=validated.client_ref,
            amount=validated.amount,
            type=validated.type,
            currency=validated.currency,
            effective_date=validated.effective_date,
            idempotency_key=validated.idempotency_key,
            description=validated.description,
            created_by=validated.created_by,
        )
        to_insert.append(entry)

    if to_insert:
        db.add_all(to_insert)
        db.commit()

    total_rows = len(batch.transactions)
    failed_rows = len(errors)
    accepted_rows = len(to_insert)

    return BatchIngestResponse(
        total_rows=total_rows,
        accepted_rows=accepted_rows,
        failed_rows=failed_rows,
        duplicate_rows=duplicate_rows,
        errors=errors,
    )


def calculate_as_of_balance(
    db: Session,
    firm_id: str,
    client_ref: str,
    as_of: datetime,
    currency: str = "USD",
) -> BalanceResponse:
    """
    Computes point-in-time balance by summing all ledger entries up to as_of date.
    Account for DEPOSITs (+), WITHDRAWALs (-), FEEs (-), and REVERSALs (canceling original direction).
    """
    entries = (
        db.query(LedgerEntry)
        .filter(
            LedgerEntry.firm_id == firm_id,
            LedgerEntry.client_ref == client_ref,
            LedgerEntry.currency == currency,
            LedgerEntry.effective_date <= as_of,
        )
        .order_by(LedgerEntry.effective_date.asc())
        .all()
    )

    balance = Decimal("0.0000")

    # Map of original entries to properly handle reversals
    reversal_lookup = {e.id: e for e in entries}

    for entry in entries:
        if entry.type == TransactionType.DEPOSIT:
            balance += entry.amount
        elif entry.type in (TransactionType.WITHDRAWAL, TransactionType.FEE):
            balance -= entry.amount
        elif entry.type == TransactionType.REVERSAL:
            # Reversal inverts the effect of original transaction
            original = reversal_lookup.get(entry.reversal_of_id)
            if original:
                if original.type == TransactionType.DEPOSIT:
                    balance -= original.amount
                elif original.type in (TransactionType.WITHDRAWAL, TransactionType.FEE):
                    balance += original.amount

    return BalanceResponse(
        firm_id=firm_id,
        client_ref=client_ref,
        currency=currency,
        as_of=as_of,
        balance=balance,
        transaction_count=len(entries),
    )


def calculate_period_summary(
    db: Session,
    firm_id: str,
    client_ref: str,
    from_date: datetime,
    to_date: datetime,
    currency: str = "USD",
) -> PeriodSummaryResponse:
    """
    Aggregates period activity: inflows, outflows, fees, and net change.
    """
    entries = (
        db.query(LedgerEntry)
        .filter(
            LedgerEntry.firm_id == firm_id,
            LedgerEntry.client_ref == client_ref,
            LedgerEntry.currency == currency,
            LedgerEntry.effective_date >= from_date,
            LedgerEntry.effective_date <= to_date,
        )
        .all()
    )

    inflows = Decimal("0.0000")
    outflows = Decimal("0.0000")
    fees = Decimal("0.0000")

    for entry in entries:
        if entry.type == TransactionType.DEPOSIT:
            inflows += entry.amount
        elif entry.type == TransactionType.WITHDRAWAL:
            outflows += entry.amount
        elif entry.type == TransactionType.FEE:
            fees += entry.amount

    net_change = inflows - outflows - fees

    return PeriodSummaryResponse(
        firm_id=firm_id,
        client_ref=client_ref,
        currency=currency,
        from_date=from_date,
        to_date=to_date,
        total_inflows=inflows,
        total_outflows=outflows,
        total_fees=fees,
        net_change=net_change,
    )


def reverse_transaction(
    db: Session,
    firm_id: str,
    transaction_id: int,
    reversal_req: ReversalRequest,
) -> LedgerEntry:
    """
    Appends an immutable compensating REVERSAL entry.
    Ensures zero DELETE or UPDATE on financial history.
    """
    original = (
        db.query(LedgerEntry)
        .filter(LedgerEntry.id == transaction_id, LedgerEntry.firm_id == firm_id)
        .first()
    )

    if not original:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Transaction #{transaction_id} not found for firm '{firm_id}'",
        )

    if original.type == TransactionType.REVERSAL:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot reverse an existing reversal entry",
        )

    # Check if already reversed
    existing_reversal = (
        db.query(LedgerEntry)
        .filter(
            LedgerEntry.firm_id == firm_id,
            LedgerEntry.reversal_of_id == transaction_id,
        )
        .first()
    )
    if existing_reversal:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Transaction #{transaction_id} has already been reversed by #{existing_reversal.id}",
        )

    reversal = LedgerEntry(
        firm_id=firm_id,
        client_ref=original.client_ref,
        amount=original.amount,
        type=TransactionType.REVERSAL,
        currency=original.currency,
        effective_date=datetime.utcnow(),
        reversal_of_id=original.id,
        description=f"Reversal of #{original.id}: {reversal_req.reason}",
        created_by=reversal_req.created_by,
    )
    db.add(reversal)
    db.commit()
    db.refresh(reversal)
    return reversal


def list_client_history(
    db: Session,
    firm_id: str,
    client_ref: str,
    limit: int = 50,
    offset: int = 0,
) -> List[LedgerEntry]:
    """
    Returns chronological immutable ledger history for an opaque client reference.
    """
    return (
        db.query(LedgerEntry)
        .filter(
            LedgerEntry.firm_id == firm_id,
            LedgerEntry.client_ref == client_ref,
        )
        .order_by(LedgerEntry.effective_date.desc(), LedgerEntry.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
