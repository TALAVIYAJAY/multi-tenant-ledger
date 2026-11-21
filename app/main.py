from contextlib import asynccontextmanager
from datetime import datetime
from typing import List, Optional
from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import get_db, init_db
from app.schemas import (
    BalanceResponse,
    BatchIngestRequest,
    BatchIngestResponse,
    LedgerEntryOut,
    PeriodSummaryResponse,
    ReversalRequest,
)
from app.service import (
    calculate_as_of_balance,
    calculate_period_summary,
    list_client_history,
    process_batch_ingest,
    reverse_transaction,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Multi-Tenant Financial Ledger API",
    description="Compliance-grade, immutable multi-tenant transaction ledger for wealth advisory firms.",
    version="1.0.0",
    lifespan=lifespan,
)


def get_current_firm(
    x_firm_id: str = Header(
        ...,
        alias="X-Firm-ID",
        description="Opaque advisory firm identifier for strict tenant isolation",
    ),
) -> str:
    """Enforces strict multi-tenant boundary on every incoming request."""
    firm_id = x_firm_id.strip()
    if not firm_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Header 'X-Firm-ID' must not be empty",
        )
    return firm_id


@app.get("/health", tags=["System"])
def health():
    return {"status": "ok", "service": "multi-tenant-ledger"}


@app.post(
    "/transactions/batch",
    response_model=BatchIngestResponse,
    status_code=status.HTTP_200_OK,
    tags=["Ingestion"],
    summary="Bulk transaction ingestion with row-level validation and deduplication",
)
def ingest_batch(
    batch: BatchIngestRequest,
    firm_id: str = Depends(get_current_firm),
    db: Session = Depends(get_db),
):
    return process_batch_ingest(db=db, firm_id=firm_id, batch=batch)


@app.get(
    "/clients/{client_ref}/balance",
    response_model=BalanceResponse,
    tags=["Reporting"],
    summary="Point-in-time client balance query (e.g. quarter-end balance)",
)
def get_client_balance(
    client_ref: str,
    as_of: Optional[datetime] = Query(
        default=None,
        description="Calculate exact historical balance up to this timestamp (ISO 8601)",
    ),
    currency: str = Query(default="USD", min_length=3, max_length=3),
    firm_id: str = Depends(get_current_firm),
    db: Session = Depends(get_db),
):
    target_as_of = as_of or datetime.utcnow()
    return calculate_as_of_balance(
        db=db,
        firm_id=firm_id,
        client_ref=client_ref,
        as_of=target_as_of,
        currency=currency.upper(),
    )


@app.get(
    "/clients/{client_ref}/summary",
    response_model=PeriodSummaryResponse,
    tags=["Reporting"],
    summary="Period financial activity summary (inflows, outflows, fees, net change)",
)
def get_period_summary(
    client_ref: str,
    from_date: datetime = Query(..., description="Start timestamp (inclusive)"),
    to_date: datetime = Query(..., description="End timestamp (inclusive)"),
    currency: str = Query(default="USD", min_length=3, max_length=3),
    firm_id: str = Depends(get_current_firm),
    db: Session = Depends(get_db),
):
    return calculate_period_summary(
        db=db,
        firm_id=firm_id,
        client_ref=client_ref,
        from_date=from_date,
        to_date=to_date,
        currency=currency.upper(),
    )


@app.get(
    "/clients/{client_ref}/history",
    response_model=List[LedgerEntryOut],
    tags=["Audit"],
    summary="Immutable chronological transaction history for an opaque client",
)
def get_client_history(
    client_ref: str,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    firm_id: str = Depends(get_current_firm),
    db: Session = Depends(get_db),
):
    return list_client_history(
        db=db,
        firm_id=firm_id,
        client_ref=client_ref,
        limit=limit,
        offset=offset,
    )


@app.post(
    "/transactions/{transaction_id}/reverse",
    response_model=LedgerEntryOut,
    status_code=status.HTTP_201_CREATED,
    tags=["Audit"],
    summary="Append-only compensating entry to reverse a mistake without rewriting history",
)
def reverse_entry(
    transaction_id: int,
    reversal: ReversalRequest,
    firm_id: str = Depends(get_current_firm),
    db: Session = Depends(get_db),
):
    return reverse_transaction(
        db=db,
        firm_id=firm_id,
        transaction_id=transaction_id,
        reversal_req=reversal,
    )
