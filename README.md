# Employee Attendance & Analytics API

FastAPI and MongoDB service for employee records, attendance punch-in/out, manual corrections with audit history, and analytics. See `app/main.py`, `REVIEW.md`, and `DECISIONS.md`.

Requirements: Python 3.11+, MongoDB 6.0+.

```bash
python -m venv .venv
# Activate the environment
pip install -r requirements.txt
# Set MONGO_URI and MONGO_DB as environment variables
uvicorn app.main:app --port 8000
```

API docs: http://127.0.0.1:8000/docs

Focused tests: `python -m unittest discover -s tests`

Full endpoint and explain-plan checks require a running MongoDB instance with the assignment's sample data loaded. No secrets or `.env` files belong in the repository.