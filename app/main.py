import json
import os
from calendar import monthrange
from datetime import date as Date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = (*PRESENCE_STATUSES, "ABSENT", "LEAVE")

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

EpochMillis = Annotated[int, Field(strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
ShiftTime = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: ShiftTime = "09:30"
    shift_end: ShiftTime = "18:30"
    joined_on: Date

    @model_validator(mode="after")
    def shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: EpochMillis = None
    status: PresenceStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: EpochMillis = None


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Status = None
    punch_in: EpochMillis = None
    punch_out: EpochMillis = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


@app.on_event("startup")
def create_indexes():
    try:
        db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_1")
        db.employees.create_index(
            [("department", ASCENDING), ("joined_on", ASCENDING)],
            name="department_1_joined_on_1",
        )
        db.employees.create_index([("joined_on", ASCENDING)], name="joined_on_1")

        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)],
            unique=True,
            name="emp_code_1_date_1",
        )
        db.attendance_logs.create_index(
            [("date", DESCENDING), ("emp_code", ASCENDING)],
            name="date_-1_emp_code_1",
        )
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", DESCENDING)],
            name="emp_code_1_date_-1",
        )
        db.attendance_logs.create_index(
            [("date", ASCENDING), ("late_minutes", DESCENDING)],
            name="date_1_late_minutes_-1",
        )
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
            name="emp_code_1_punch_in_-1",
        )
    except PyMongoError:
        return


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def epoch_to_datetime(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def datetime_to_epoch(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.astimezone(UTC).timestamp()) * 1000


def api_value(value):
    if isinstance(value, datetime):
        return datetime_to_epoch(value)
    if isinstance(value, dict):
        return {key: api_value(item) for key, item in value.items() if key != "_id"}
    if isinstance(value, list):
        return [api_value(item) for item in value]
    return value


def employee_response(doc: dict) -> dict:
    result = {key: value for key, value in doc.items() if key != "_id"}
    return api_value(result)


def attendance_response(doc: dict) -> dict:
    result = {key: value for key, value in doc.items() if key != "_id"}
    result.setdefault("punch_in", None)
    result.setdefault("punch_out", None)
    result.setdefault("work_hours", None)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)
    result.setdefault("history", [])
    return api_value(result)


def attendance_date_for(punch_in: datetime, employee: dict) -> Date:
    local = punch_in.astimezone(IST)
    local_day = local.date()
    if employee["shift_end"] <= employee["shift_start"] and local.strftime("%H:%M") < employee["shift_end"]:
        return local_day - timedelta(days=1)
    return local_day


def late_minutes(punch_in: datetime, shift_start: str, attendance_day: str | Date) -> int:
    day = Date.fromisoformat(attendance_day) if isinstance(attendance_day, str) else attendance_day
    shift_hour = time.fromisoformat(shift_start)
    shift_start_at = datetime.combine(day, shift_hour, tzinfo=IST).astimezone(UTC)
    elapsed = int((punch_in - shift_start_at).total_seconds())
    if elapsed > 10 * 60:
        return elapsed // 60
    return 0


def work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    value = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(value)


def overtime_minutes(punch_out: datetime, shift_start: str, shift_end: str, attendance_day: str | Date) -> int:
    day = Date.fromisoformat(attendance_day) if isinstance(attendance_day, str) else attendance_day
    end_day = day + timedelta(days=1) if shift_end <= shift_start else day
    end_at = datetime.combine(end_day, time.fromisoformat(shift_end), tzinfo=IST).astimezone(UTC)
    elapsed = int((punch_out - end_at).total_seconds())
    return elapsed // 60 if elapsed >= 30 * 60 else 0


def attendance_query(
    emp_code: str | None,
    date_from: Date | None,
    date_to: Date | None,
    status: str | None,
) -> dict:
    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from.isoformat()
        if date_to:
            query["date"]["$lte"] = date_to.isoformat()
    if status:
        query["status"] = status
    return query


def month_bounds(month: str) -> tuple[str, str, str]:
    year, month_number = map(int, month.split("-"))
    try:
        last_day = monthrange(year, month_number)[1]
        next_month_start = Date(year + (month_number == 12), 1 if month_number == 12 else month_number + 1, 1)
    except ValueError:
        raise HTTPException(status_code=422, detail="month must be a valid calendar month")
    return f"{month}-01", f"{month}-{last_day:02d}", next_month_start.isoformat()


def round_half_up_expr(value, places: int):
    factor = 10**places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [value, factor]}, 0.5]}}, factor]}


def run_aggregate(collection, pipeline: list[dict], hint: str | dict | None = None) -> list[dict]:
    options = {"allowDiskUse": True}
    if hint is not None:
        options["hint"] = hint
    return list(collection.aggregate(pipeline, **options))


def explain_aggregate(collection, pipeline: list[dict], hint: str | dict | None = None) -> dict:
    command = {"aggregate": collection.name, "pipeline": pipeline, "cursor": {}, "allowDiskUse": True}
    if hint is not None:
        command["hint"] = hint
    return db.command("explain", command, verbosity="executionStats")


def day_of_week(date_expr):
    return {"$dayOfWeek": {"$dateFromString": {"dateString": date_expr}}}


def month_attendance_lookup(emp_code_expr: str, month_start: str, month_end: str) -> list[dict]:
    return [
        {
            "$match": {
                "$expr": {
                    "$and": [
                        {"$eq": ["$emp_code", emp_code_expr]},
                        {"$gte": ["$date", month_start]},
                        {"$lte": ["$date", month_end]},
                    ]
                }
            }
        }
    ]


def employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    month_start, month_end, next_month_start = month_bounds(month)
    lookup_pipeline = month_attendance_lookup("$$code", month_start, month_end) + [
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {"$in": [day_of_week("$date"), [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        }
    ]
    effective_start = {
        "$dateFromString": {
            "dateString": {"$cond": [{"$gt": ["$joined_on", month_start]}, "$joined_on", month_start]}
        }
    }
    days_count = {"$dateDiff": {"startDate": effective_start, "endDate": {"$dateFromString": {"dateString": next_month_start}}, "unit": "day"}}
    return [
        {"$match": {"emp_code": emp_code}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": lookup_pipeline,
                "as": "month_stats",
            }
        },
        {
            "$set": {
                "_working_days": {
                    "$size": {
                        "$filter": {
                            "input": {"$range": [0, {"$max": [0, days_count]}]},
                            "as": "offset",
                            "cond": {
                                "$in": [
                                    {
                                        "$dayOfWeek": {
                                            "$dateAdd": {"startDate": effective_start, "unit": "day", "amount": "$$offset"}
                                        }
                                    },
                                    [2, 3, 4, 5, 6],
                                ]
                            },
                        }
                    }
                },
                "_stats": {"$ifNull": [{"$arrayElemAt": ["$month_stats", 0]}, {}]},
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "month": {"$literal": month},
                "working_days": "$_working_days",
                "present_days": {"$ifNull": ["$_stats.present_days", 0]},
                "leave_days": {"$ifNull": ["$_stats.leave_days", 0]},
                "late_count": {"$ifNull": ["$_stats.late_count", 0]},
                "total_late_minutes": {"$ifNull": ["$_stats.total_late_minutes", 0]},
                "total_overtime_minutes": {"$ifNull": ["$_stats.total_overtime_minutes", 0]},
                "attendance_pct": {
                    "$cond": [
                        {"$gt": ["$_working_days", 0]},
                        round_half_up_expr({"$multiply": [{"$divide": [{"$ifNull": ["$_stats.present_days", 0]}, "$_working_days"]}, 100]}, 4),
                        None,
                    ]
                },
            }
        },
    ]


def department_summary_pipeline(month: str, department: str | None) -> list[dict]:
    month_start, month_end, _ = month_bounds(month)
    employee_match = {"joined_on": {"$lte": month_end}}
    if department is not None:
        employee_match["department"] = department
    lookup_pipeline = month_attendance_lookup("$$code", month_start, month_end) + [
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {"$in": [day_of_week("$date"), [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                                ]
                            },
                            "$work_hours",
                            0,
                        ]
                    }
                },
                "hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            }
        }
    ]
    hint = "department_1_joined_on_1" if department is not None else "joined_on_1"
    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": lookup_pipeline,
                "as": "month_stats",
            }
        },
        {"$set": {"_stats": {"$ifNull": [{"$arrayElemAt": ["$month_stats", 0]}, {}]}}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": {"$ifNull": ["$_stats.present_days", 0]}},
                "hours_sum": {"$sum": {"$ifNull": ["$_stats.hours_sum", 0]}},
                "hours_count": {"$sum": {"$ifNull": ["$_stats.hours_count", 0]}},
                "late_count": {"$sum": {"$ifNull": ["$_stats.late_count", 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$_stats.total_late_minutes", 0]}},
                "leave_count": {"$sum": {"$ifNull": ["$_stats.leave_count", 0]}},
                "on_duty_count": {"$sum": {"$ifNull": ["$_stats.on_duty_count", 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$hours_count", 0]},
                        round_half_up_expr({"$divide": ["$hours_sum", "$hours_count"]}, 2),
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]
    return pipeline


def late_leaderboard_pipeline(month: str, department: str | None, limit: int) -> list[dict]:
    month_start, month_end, _ = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"code": "$_id"},
                "pipeline": [{"$match": {"$expr": {"$eq": ["$emp_code", "$$code"]}}}],
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


def department_trend_pipeline(department: str, start: Date, end: Date) -> list[dict]:
    days = (end - start).days + 1
    start_string = start.isoformat()
    pipeline = [
        {"$match": {"department": department}},
        {
            "$set": {
                "_trend_dates": {
                    "$map": {
                        "input": {"$range": [0, days]},
                        "as": "offset",
                        "in": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": {
                                    "$dateAdd": {
                                        "startDate": {"$dateFromString": {"dateString": start_string}},
                                        "unit": "day",
                                        "amount": "$$offset",
                                    }
                                },
                            }
                        },
                    }
                }
            }
        },
        {"$unwind": "$_trend_dates"},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code", "day": "$_trend_dates"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$eq": ["$date", "$$day"]}]}}},
                    {"$project": {"_id": 0, "status": 1, "half_day": 1, "late_minutes": 1}},
                ],
                "as": "_daily_log",
            }
        },
        {"$set": {"_record": {"$arrayElemAt": ["$_daily_log", 0]}}},
        {
            "$group": {
                "_id": "$_trend_dates",
                "headcount": {"$sum": {"$cond": [{"$lte": ["$joined_on", "$_trend_dates"]}, 1, 0]}},
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$_record.status", list(PRESENCE_STATUSES)]},
                            {"$cond": [{"$ifNull": ["$_record.half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$_record.late_minutes", 0]}, 0]}, 1, 0]}
                },
            }
        },
        {
            "$set": {
                "is_working_day": {"$in": [day_of_week("$_id"), [2, 3, 4, 5, 6]]},
                "attendance_rate": {
                    "$cond": [
                        {"$and": [{"$in": [day_of_week("$_id"), [2, 3, 4, 5, 6]]}, {"$gt": ["$headcount", 0]}]},
                        round_half_up_expr({"$divide": ["$present_count", "$headcount"]}, 4),
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {
                    "_moving_avg": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$_moving_avg", None]},
                        None,
                        round_half_up_expr("$_moving_avg", 4),
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]
    return pipeline


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB is unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["joined_on"] = body.joined_on.isoformat()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return employee_response(doc)


@app.get("/employees")
def list_employees(
    department: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_response(doc) for doc in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")

    punched_at = epoch_to_datetime(body.punched_at) if body.punched_at is not None else now_utc()
    attendance_day = attendance_date_for(punched_at, employee).isoformat()
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_day,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes(punched_at, employee["shift_start"], attendance_day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return attendance_response(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")

    punched_at = epoch_to_datetime(body.punched_at) if body.punched_at is not None else now_utc()
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        any_record = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
            sort=[("punch_in", DESCENDING)],
        )
        if any_record is not None and punched_at <= any_record["punch_in"]:
            raise HTTPException(status_code=422, detail="punched_at must be after punch_in")
        raise HTTPException(status_code=404, detail="no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record is already punched out")
    if punched_at <= record["punch_in"]:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")
    if punched_at - record["punch_in"] > timedelta(hours=24):
        raise HTTPException(status_code=422, detail="punch duration cannot exceed 24 hours")

    hours = work_hours(record["punch_in"], punched_at)
    values = {
        "punch_out": punched_at,
        "work_hours": hours,
        "overtime_minutes": overtime_minutes(punched_at, employee["shift_start"], employee["shift_end"], record["date"]),
        "half_day": hours < 4.50,
    }
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None, "punch_in": record["punch_in"]},
        {"$set": values},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record is already punched out")
    return attendance_response(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: Date | None = None,
    date_to: Date | None = None,
    status: Status | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
    query = attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [attendance_response(doc) for doc in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: Date, body: RegularizeRequest):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date.isoformat()})
    if old is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    provided = body.model_fields_set
    requested_status = body.status if "status" in provided else old.get("status")
    if requested_status in ("ABSENT", "LEAVE") and ("punch_in" in provided or "punch_out" in provided):
        raise HTTPException(status_code=422, detail="ABSENT and LEAVE records cannot include punch times")

    new_in = epoch_to_datetime(body.punch_in) if "punch_in" in provided else old.get("punch_in")
    new_out = epoch_to_datetime(body.punch_out) if "punch_out" in provided else old.get("punch_out")
    if requested_status in ("ABSENT", "LEAVE"):
        new_in, new_out = None, None
    else:
        if new_in is None:
            raise HTTPException(status_code=422, detail="presence status requires punch_in")
        if attendance_date_for(new_in, employee).isoformat() != date.isoformat():
            raise HTTPException(status_code=422, detail="punch_in must belong to the record's attendance date")
        if new_out is not None:
            if new_out <= new_in:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if new_out - new_in > timedelta(hours=24):
                raise HTTPException(status_code=422, detail="punch duration cannot exceed 24 hours")

    if requested_status in ("ABSENT", "LEAVE"):
        computed = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    else:
        computed = {
            "work_hours": work_hours(new_in, new_out) if new_out is not None else None,
            "late_minutes": late_minutes(new_in, employee["shift_start"], date),
            "overtime_minutes": overtime_minutes(new_out, employee["shift_start"], employee["shift_end"], date) if new_out is not None else 0,
            "half_day": work_hours(new_in, new_out) < 4.50 if new_out is not None else False,
        }
    new_values = {
        "status": requested_status,
        "punch_in": new_in,
        "punch_out": new_out,
        **computed,
    }
    old_values = {
        "status": old.get("status"),
        "punch_in": old.get("punch_in"),
        "punch_out": old.get("punch_out"),
        "work_hours": old.get("work_hours"),
        "late_minutes": old.get("late_minutes", 0),
        "overtime_minutes": old.get("overtime_minutes", 0),
        "half_day": old.get("half_day", False),
    }
    changes = {}
    for key, new_value in new_values.items():
        old_value = old_values[key]
        if old_value != new_value:
            changes[key] = {"from": old_value, "to": new_value}
    if not changes:
        raise HTTPException(status_code=422, detail="regularization must change at least one field")

    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    query = {"_id": old["_id"], "emp_code": emp_code, "date": date.isoformat()}
    compare_fields = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day", "history")
    conditions = []
    for key in compare_fields:
        if key in old:
            conditions.append({key: old[key]})
        else:
            conditions.append({key: {"$exists": False}})
    query["$and"] = conditions
    updated = db.attendance_logs.find_one_and_update(
        query,
        {"$set": new_values, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record changed during regularization; retry")
    return attendance_response(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    pipeline = employee_monthly_pipeline(emp_code, month)
    rows = run_aggregate(db.employees, pipeline, hint="emp_code_1")
    if not rows:
        raise HTTPException(status_code=404, detail="employee not found")
    return rows[0]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: str | None = None,
):
    pipeline = department_summary_pipeline(month, department)
    hint = "department_1_joined_on_1" if department is not None else "joined_on_1"
    rows = run_aggregate(db.employees, pipeline, hint=hint)
    return {"month": month, "items": rows}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: str | None = None,
):
    pipeline = late_leaderboard_pipeline(month, department, limit)
    rows = run_aggregate(db.attendance_logs, pipeline, hint="date_1_late_minutes_-1")
    return {"month": month, "items": rows}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: Date = Query(alias="from"), to_date: Date = Query(alias="to")):
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(status_code=422, detail="date range cannot exceed 92 days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="department not found")
    pipeline = department_trend_pipeline(department, from_date, to_date)
    rows = run_aggregate(db.employees, pipeline, hint="department_1_joined_on_1")
    return {"department": department, "items": rows}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: str | None = None,
    month: str | None = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: str | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Date | None = None,
    date_to: Date | None = None,
    status: Status | None = None,
    from_date: Date | None = Query(default=None, alias="from"),
    to_date: Date | None = Query(default=None, alias="to"),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
        query = attendance_query(emp_code, date_from, date_to, status)
        explanation = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        pipeline = employee_monthly_pipeline(emp_code, month)
        explanation = explain_aggregate(db.employees, pipeline, "emp_code_1")
        collection = "employees"
    elif endpoint == "department_summary":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = department_summary_pipeline(month, department)
        hint = "department_1_joined_on_1" if department is not None else "joined_on_1"
        explanation = explain_aggregate(db.employees, pipeline, hint)
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = late_leaderboard_pipeline(month, department, limit)
        explanation = explain_aggregate(db.attendance_logs, pipeline, "date_1_late_minutes_-1")
        collection = "attendance_logs"
    else:
        if department is None or from_date is None or to_date is None:
            raise HTTPException(status_code=422, detail="department, from and to are required")
        if to_date < from_date or (to_date - from_date).days + 1 > 92:
            raise HTTPException(status_code=422, detail="invalid date range")
        pipeline = department_trend_pipeline(department, from_date, to_date)
        explanation = explain_aggregate(db.employees, pipeline, "department_1_joined_on_1")
        collection = "employees"
    json_ready = json.loads(json_util.dumps(explanation))
    return {"endpoint": endpoint, "collection": collection, "explain": json_ready}