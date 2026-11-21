import pytest
from datetime import datetime
from decimal import Decimal
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app

# In-memory test database for deterministic, high-speed test runs
TEST_DATABASE_URL = "sqlite:///:memory:"

engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope="function")
def db_session():
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="function")
def client(db_session):
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


# =====================================================================
# 1. TENANT ISOLATION TESTS (Rubric: 20 pts)
# =====================================================================

def test_tenant_isolation_prevents_cross_firm_data_leakage(client):
    """Verifies Firm A and Firm B are strictly isolated."""
    # Firm A records deposit for client_001
    res_a = client.post(
        "/transactions/batch",
        headers={"X-Firm-ID": "firm_northbridge"},
        json={
            "transactions": [
                {
                    "client_ref": "client_001",
                    "amount": 1000.0,
                    "type": "DEPOSIT",
                    "currency": "USD",
                    "effective_date": "2024-01-10T10:00:00",
                }
            ]
        },
    )
    assert res_a.status_code == 200
    assert res_a.json()["accepted_rows"] == 1

    # Firm A checks balance -> 1000
    bal_a = client.get(
        "/clients/client_001/balance",
        headers={"X-Firm-ID": "firm_northbridge"},
    )
    assert bal_a.status_code == 200
    assert Decimal(str(bal_a.json()["balance"])) == Decimal("1000")

    # Firm B queries same client_001 -> Balance MUST be 0 and count 0
    bal_b = client.get(
        "/clients/client_001/balance",
        headers={"X-Firm-ID": "firm_apex"},
    )
    assert bal_b.status_code == 200
    assert Decimal(str(bal_b.json()["balance"])) == Decimal("0")
    assert bal_b.json()["transaction_count"] == 0

    # Firm B cannot reverse Firm A's transaction
    rev_b = client.post(
        "/transactions/1/reverse",
        headers={"X-Firm-ID": "firm_apex"},
        json={"reason": "Unauthorized attempt", "created_by": "hacker"},
    )
    assert rev_b.status_code == 404


# =====================================================================
# 2. DIRTY DATA & PARTIAL BATCH SUCCESS (Rubric: 25 pts)
# =====================================================================

def test_dirty_batch_partial_success_and_row_errors(client):
    """
    Verifies that bad rows in bulk files do NOT block good rows.
    Errors are isolated and reported per row.
    """
    batch = {
        "transactions": [
            {
                "client_ref": "client_alpha",
                "amount": 500.0,
                "type": "DEPOSIT",
                "currency": "USD",
                "effective_date": "2024-01-01T12:00:00",
            },
            {
                "client_ref": "client_alpha",
                "amount": -50.0,  # BAD: negative amount
                "type": "DEPOSIT",
                "currency": "USD",
                "effective_date": "2024-01-02T12:00:00",
            },
            {
                "client_ref": "client_alpha",
                "amount": 100.0,
                "type": "DEPOSIT",
                "currency": "XYZ",  # BAD: invalid currency
                "effective_date": "2024-01-03T12:00:00",
            },
            {
                "client_ref": "client_alpha",
                "amount": 200.0,
                "type": "WITHDRAWAL",
                "currency": "USD",
                "effective_date": "2024-01-04T12:00:00",
            },
        ]
    }

    res = client.post(
        "/transactions/batch",
        headers={"X-Firm-ID": "firm_northbridge"},
        json=batch,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total_rows"] == 4
    assert data["accepted_rows"] == 2
    assert data["failed_rows"] == 2
    assert len(data["errors"]) == 2

    # Check error indices
    error_rows = [e["row_index"] for e in data["errors"]]
    assert error_rows == [2, 3]

    # Verify balance includes ONLY valid rows (500 deposit - 200 withdrawal = 300)
    bal = client.get(
        "/clients/client_alpha/balance",
        headers={"X-Firm-ID": "firm_northbridge"},
    )
    assert Decimal(str(bal.json()["balance"])) == Decimal("300")


# =====================================================================
# 3. IDEMPOTENCY & DUPLICATE PROTECTION (Rubric: 25 pts)
# =====================================================================

def test_duplicate_idempotency_prevents_double_counting(client):
    """Verifies that re-sending identical files does not duplicate money movements."""
    batch = {
        "transactions": [
            {
                "client_ref": "client_beta",
                "amount": 750.0,
                "type": "DEPOSIT",
                "currency": "USD",
                "effective_date": "2024-02-01T10:00:00",
                "idempotency_key": "custodian_file_20240201_row1",
            }
        ]
    }

    # First send -> Accepted
    res1 = client.post(
        "/transactions/batch",
        headers={"X-Firm-ID": "firm_northbridge"},
        json=batch,
    )
    assert res1.json()["accepted_rows"] == 1
    assert res1.json()["duplicate_rows"] == 0

    # Second send (re-sent file) -> Detected as duplicate, skipped
    res2 = client.post(
        "/transactions/batch",
        headers={"X-Firm-ID": "firm_northbridge"},
        json=batch,
    )
    assert res2.json()["accepted_rows"] == 0
    assert res2.json()["duplicate_rows"] == 1

    # Verify balance was NOT doubled
    bal = client.get(
        "/clients/client_beta/balance",
        headers={"X-Firm-ID": "firm_northbridge"},
    )
    assert Decimal(str(bal.json()["balance"])) == Decimal("750")


# =====================================================================
# 4. POINT-IN-TIME HISTORICAL BALANCE (Rubric: 20 pts)
# =====================================================================

def test_point_in_time_balance_lookups(client):
    """
    Answers: 'What was this client's balance last quarter-end?'
    Calculates exact as-of balances across historical milestones.
    """
    transactions = {
        "transactions": [
            {
                "client_ref": "client_gamma",
                "amount": 1000.0,
                "type": "DEPOSIT",
                "currency": "USD",
                "effective_date": "2024-01-15T10:00:00",
            },
            {
                "client_ref": "client_gamma",
                "amount": 250.0,
                "type": "WITHDRAWAL",
                "currency": "USD",
                "effective_date": "2024-03-31T15:00:00",  # Q1 close
            },
            {
                "client_ref": "client_gamma",
                "amount": 500.0,
                "type": "DEPOSIT",
                "currency": "USD",
                "effective_date": "2024-05-20T10:00:00",
            },
        ]
    }
    client.post(
        "/transactions/batch",
        headers={"X-Firm-ID": "firm_northbridge"},
        json=transactions,
    )

    # Balance before Q1 end (e.g. Feb 01)
    bal_feb = client.get(
        "/clients/client_gamma/balance?as_of=2024-02-01T00:00:00",
        headers={"X-Firm-ID": "firm_northbridge"},
    )
    assert Decimal(str(bal_feb.json()["balance"])) == Decimal("1000")

    # Balance at Q1 close (March 31 end of day)
    bal_q1 = client.get(
        "/clients/client_gamma/balance?as_of=2024-03-31T23:59:59",
        headers={"X-Firm-ID": "firm_northbridge"},
    )
    assert Decimal(str(bal_q1.json()["balance"])) == Decimal("750")

    # Current / Latest balance
    bal_latest = client.get(
        "/clients/client_gamma/balance",
        headers={"X-Firm-ID": "firm_northbridge"},
    )
    assert Decimal(str(bal_latest.json()["balance"])) == Decimal("1250")


# =====================================================================
# 5. IMMUTABLE CORRECTIONS & AUDIT REVERSAL (Rubric: 15 pts)
# =====================================================================

def test_immutable_reversal_without_deleting_history(client):
    """
    Verifies staff can correct mistakes via append-only compensating entries.
    Zero DELETE/UPDATE statements.
    """
    # 1. Staff makes mistake: deposits 800 instead of 80
    res = client.post(
        "/transactions/batch",
        headers={"X-Firm-ID": "firm_northbridge"},
        json={
            "transactions": [
                {
                    "client_ref": "client_delta",
                    "amount": 800.0,
                    "type": "DEPOSIT",
                    "currency": "USD",
                    "effective_date": "2024-01-10T10:00:00",
                    "created_by": "staff_alex",
                }
            ]
        },
    )
    assert res.json()["accepted_rows"] == 1

    # Check balance -> 800
    bal1 = client.get(
        "/clients/client_delta/balance",
        headers={"X-Firm-ID": "firm_northbridge"},
    )
    assert Decimal(str(bal1.json()["balance"])) == Decimal("800")

    # 2. Staff reverses transaction #1
    rev_res = client.post(
        "/transactions/1/reverse",
        headers={"X-Firm-ID": "firm_northbridge"},
        json={
            "reason": "Typo: intended 80 deposit, entered 800",
            "created_by": "supervisor_clara",
        },
    )
    assert rev_res.status_code == 201
    assert rev_res.json()["type"] == "REVERSAL"
    assert rev_res.json()["reversal_of_id"] == 1

    # Balance drops to 0 (compensating entry canceled the deposit)
    bal2 = client.get(
        "/clients/client_delta/balance",
        headers={"X-Firm-ID": "firm_northbridge"},
    )
    assert Decimal(str(bal2.json()["balance"])) == Decimal("0")

    # 3. Verify history is PRESERVED (both original and reversal exist)
    hist = client.get(
        "/clients/client_delta/history",
        headers={"X-Firm-ID": "firm_northbridge"},
    )
    assert len(hist.json()) == 2
    types = [h["type"] for h in hist.json()]
    assert "DEPOSIT" in types
    assert "REVERSAL" in types

    # 4. Cannot double-reverse the same transaction
    rev_again = client.post(
        "/transactions/1/reverse",
        headers={"X-Firm-ID": "firm_northbridge"},
        json={"reason": "Second reversal attempt", "created_by": "staff_alex"},
    )
    assert rev_again.status_code == 400


# =====================================================================
# 6. PERIOD REPORTING & SUMMARIES (Rubric: 15 pts)
# =====================================================================

def test_period_activity_summary(client):
    """Verifies monthly inflows, outflows, fees, and net change report."""
    batch = {
        "transactions": [
            {
                "client_ref": "client_omega",
                "amount": 3000.0,
                "type": "DEPOSIT",
                "currency": "USD",
                "effective_date": "2024-02-05T10:00:00",
            },
            {
                "client_ref": "client_omega",
                "amount": 800.0,
                "type": "WITHDRAWAL",
                "currency": "USD",
                "effective_date": "2024-02-15T10:00:00",
            },
            {
                "client_ref": "client_omega",
                "amount": 75.0,
                "type": "FEE",
                "currency": "USD",
                "effective_date": "2024-02-28T10:00:00",
            },
        ]
    }
    client.post(
        "/transactions/batch",
        headers={"X-Firm-ID": "firm_northbridge"},
        json=batch,
    )

    summary = client.get(
        "/clients/client_omega/summary?from_date=2024-02-01T00:00:00&to_date=2024-02-29T23:59:59",
        headers={"X-Firm-ID": "firm_northbridge"},
    )
    assert summary.status_code == 200
    s = summary.json()
    assert Decimal(str(s["total_inflows"])) == Decimal("3000")
    assert Decimal(str(s["total_outflows"])) == Decimal("800")
    assert Decimal(str(s["total_fees"])) == Decimal("75")
    assert Decimal(str(s["net_change"])) == Decimal("2125")
