# AI Usage Disclosure

In accordance with the challenge guidelines, AI tools were utilized as an engineering accelerator during the design, coding, and testing of this challenge.

### 1. Tools Used
- **Claude / Gemini (Antigravity IDE)**: Architecture framing, schema boilerplate generation, and test scenario drafting.

### 2. Key Prompts
- *"Design an append-only SQLAlchemy ledger schema supporting multi-tenancy, strict Decimal arithmetic, and unique idempotency keys."*
- *"Implement a FastAPI batch ingestion pattern that allows partial success (row-level validation) so one dirty row does not block valid rows."*
- *"Generate comprehensive pytest test cases for multi-tenant data leakage, point-in-time balance queries, and compensating reversal state transitions."*

### 3. Decisions, Corrections & Rejected AI Suggestions
- **Rejected Batch Schema**: The AI initially suggested validating the batch via `transactions: List[TransactionCreate]`. I rejected this because FastAPI/Pydantic would reject the entire HTTP payload if row #45 had a typo (reproducing Northbridge's exact all-or-nothing pain point). I modified it to accept raw row dictionaries and validate row-by-row in the service layer, returning explicit row-index error diagnostics while safely committing valid rows.
- **Corrected Reversal Financial Logic**: The AI initially drafted reversals as negative numbers. I corrected the logic to reference the original transaction ID and invert its specific transaction type effect (e.g., reversing a fee or withdrawal credits the account, whereas reversing a deposit debits the account).
- **Rejected Complex FX Mocking**: The AI proposed an external currency exchange rate service. I rejected this to avoid external network dependencies and latency risks, keeping the focus squarely on core ledger correctness, idempotency, and tenant isolation within the 60-minute time constraint.
