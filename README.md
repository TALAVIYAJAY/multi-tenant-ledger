# Multi-Tenant Ledger Service
python -m venv venv && source venv/bin/activate  # Windows: .\venv\Scripts\activate
pip install -r requirements.txt
pytest
uvicorn app.main:app --port 8000
