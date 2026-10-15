# Employee Attendance & Analytics API

FastAPI service backed by MongoDB. All application endpoints are implemented in `app/main.py`; database indexes are created at startup.

## Run locally

Requires Python 3.11+ and MongoDB 6.0+.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export MONGO_URI='mongodb://localhost:27017'
export MONGO_DB='attendance_db'
uvicorn app.main:app --port 8000
```

On Windows PowerShell, use `$env:MONGO_URI="mongodb://localhost:27017"` and `$env:MONGO_DB="attendance_db"` instead of `export`.

`GET /health` pings MongoDB. Request/response instants use epoch milliseconds; stored BSON datetimes are UTC. Analytics are calculated in MongoDB aggregation pipelines. The service expects the `employees` and `attendance_logs` collections described in the assignment data model.

Run the focused rule tests with `python -m unittest discover -s tests`. Full endpoint and explain-plan checks require a running MongoDB instance with the assignment data loaded.