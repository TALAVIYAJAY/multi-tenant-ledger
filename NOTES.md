1. Chosen Problems: Solved dirty bulk ingestion, silent duplicates, missing historical balances, immutable auditability, and cross-firm data isolation.
2. Problem Rationale: In wealth management, data leaks destroy firms and all-or-nothing ingest stalls operations; reliability and trust come first.
3. Architecture: Implemented an append-only double-entry ledger with zero UPDATE/DELETE queries, strict Decimal math, and X-Firm-ID tenant scoping.
4. Duplicate Risk: Idempotency keys deduplicate re-sent custodian files both in-batch and against persistent state without double-billing.
5. Ingestion Risk: Row-level validation isolates dirty rows (negative amounts, bad currencies), recording line-item errors while committing valid data.
6. Skipped in 1-Hour: Real-time FX multi-currency conversions and complex multi-role RBAC permissions (prioritized ledger correctness).
7. Concurrency Safety: Handled via atomic SQLite transactions; production PostgreSQL uses SELECT FOR UPDATE and unique constraints on (firm_id, idempotency_key).
8. What Breaks at 100x Load: Direct SUM() queries on historical ledger entries will degrade performance as client transaction histories reach millions.
9. 100x Remediation: Introduce periodic materialized balance snapshots (daily/quarter-end checkpoints) plus delta rollups to achieve O(1) balance lookups.
10. Audit Compliance: All reversals create compensating entries linked to the original record with operator ID (created_by) and mandatory business reason.
