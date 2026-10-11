"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from bson import json_util
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator
from pymongo import MongoClient
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH_MILLIS_MIN = 100_000_000_000
EPOCH_MILLIS_MAX = 4_102_444_800_000
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = (*PRESENCE_STATUSES, "ABSENT", "LEAVE")

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def as_utc(value: datetime) -> datetime:
    """Treat naive BSON datetimes as UTC and normalize aware values to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def truncate_to_seconds(value: datetime) -> datetime:
    """Discard fractional seconds before storing or calculating an instant."""
    return as_utc(value).replace(microsecond=0)


def epoch_millis_to_datetime(value: int) -> datetime:
    """Convert a validated epoch-millisecond instant into a UTC BSON datetime."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("timestamp must be an integer number of milliseconds")
    if not EPOCH_MILLIS_MIN <= value <= EPOCH_MILLIS_MAX:
        raise ValueError("timestamp is outside the supported epoch-millisecond range")
    return datetime.fromtimestamp(value // 1000, tz=UTC)


def datetime_to_epoch_millis(value: datetime) -> int:
    """Convert an instant to whole-second epoch milliseconds."""
    instant = truncate_to_seconds(value)
    return int(instant.timestamp()) * 1000


def shift_time(value: str) -> time:
    """Parse the contract's validated HH:MM shift time."""
    hour, minute = map(int, value.split(":"))
    return time(hour, minute)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    """Return the IST shift-start date, including early punches on overnight shifts."""
    local_punch = truncate_to_seconds(punch_in).astimezone(IST)
    start = shift_time(shift_start)
    end = shift_time(shift_end)
    day = local_punch.date()
    if end <= start and local_punch.time().replace(tzinfo=None) < end:
        return day - timedelta(days=1)
    return day


def _round_half_up(value: Decimal, places: int) -> Decimal:
    """Apply the assignment's decimal half-up rounding rule."""
    quantum = Decimal(1).scaleb(-places)
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    shift_end: Optional[str] = None,
) -> int:
    """Compute lateness from the applicable IST shift start, with a 10-minute grace."""
    local_punch = truncate_to_seconds(punch_in).astimezone(IST)
    start_time = shift_time(shift_start)
    shift_day = local_punch.date()
    if shift_end is not None and shift_time(shift_end) <= start_time:
        if local_punch.time().replace(tzinfo=None) < shift_time(shift_end):
            shift_day -= timedelta(days=1)
    shift_start_at = datetime.combine(shift_day, start_time, tzinfo=IST)
    elapsed_seconds = int((local_punch - shift_start_at).total_seconds())
    if elapsed_seconds <= 10 * 60:
        return 0
    return elapsed_seconds // 60


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """Calculate elapsed hours and round using decimal half-up semantics."""
    start = truncate_to_seconds(punch_in)
    end = truncate_to_seconds(punch_out)
    seconds = int((end - start).total_seconds())
    hours = _round_half_up(Decimal(seconds) / Decimal(3600), 2)
    return float(hours)


def compute_overtime(
    punch_out: datetime,
    shift_end: str,
    date_str: str,
    shift_start: str,
) -> int:
    """Return whole overtime minutes only when the 30-minute minimum is met."""
    attendance_day = date.fromisoformat(date_str)
    end_time = shift_time(shift_end)
    end_day = attendance_day
    if end_time <= shift_time(shift_start):
        end_day += timedelta(days=1)
    shift_end_at = datetime.combine(end_day, end_time, tzinfo=IST)
    elapsed_seconds = int((truncate_to_seconds(punch_out).astimezone(IST) - shift_end_at).total_seconds())
    overtime_minutes = max(0, elapsed_seconds // 60)
    return overtime_minutes if overtime_minutes >= 30 else 0


def create_indexes() -> None:
    """Create query and uniqueness indexes safely on every application startup."""
    db.employees.create_index([("emp_code", 1)], unique=True, name="uq_employees_emp_code")
    db.employees.create_index(
        [("department", 1), ("joined_on", 1)],
        name="ix_employees_department_joined_on",
    )
    db.employees.create_index(
        [("joined_on", 1), ("department", 1)],
        name="ix_employees_joined_on_department",
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uq_attendance_emp_code_date",
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("punch_in", -1)],
        name="ix_attendance_emp_code_punch_in",
    )
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="ix_attendance_date_emp_code",
    )
    db.attendance_logs.create_index(
        [("date", 1), ("status", 1), ("emp_code", 1)],
        name="ix_attendance_date_status_emp_code",
    )


@app.on_event("startup")
def initialize_database() -> None:
    """Fail startup visibly if the contract's required indexes cannot be created."""
    create_indexes()


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
EpochMillis = Annotated[
    StrictInt,
    Field(ge=EPOCH_MILLIS_MIN, le=EPOCH_MILLIS_MAX),
]


def _is_valid_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return value == date.fromisoformat(value).isoformat()


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: Annotated[StrictStr, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[StrictStr, Field(min_length=1, max_length=100)]
    email: Annotated[StrictStr, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[StrictStr, Field(min_length=1, max_length=50)]
    shift_start: Annotated[StrictStr, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] = "09:30"
    shift_end: Annotated[StrictStr, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] = "18:30"
    joined_on: Annotated[StrictStr, Field()]

    @model_validator(mode="after")
    def validate_employee_dates_and_shifts(self) -> "EmployeeIn":
        if not _is_valid_date(self.joined_on):
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: StrictStr
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: StrictStr
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: Annotated[StrictStr, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[StrictStr, Field(min_length=1, max_length=50)]


def attendance_response(document: dict) -> dict:
    """Convert stored attendance BSON values to the public API representation."""
    return {
        "emp_code": document["emp_code"],
        "date": document["date"],
        "status": document["status"],
        "punch_in": (
            datetime_to_epoch_millis(document["punch_in"])
            if document.get("punch_in") is not None
            else None
        ),
        "punch_out": (
            datetime_to_epoch_millis(document["punch_out"])
            if document.get("punch_out") is not None
            else None
        ),
        "work_hours": document.get("work_hours"),
        "late_minutes": document.get("late_minutes", 0),
        "overtime_minutes": document.get("overtime_minutes", 0),
        "half_day": document.get("half_day", False),
        "history": [
            {
                "at": datetime_to_epoch_millis(entry["at"]),
                "by": entry["by"],
                "reason": entry["reason"],
                "changes": {
                    key: {
                        "from": datetime_to_epoch_millis(change["from"])
                        if isinstance(change.get("from"), datetime)
                        else change.get("from"),
                        "to": datetime_to_epoch_millis(change["to"])
                        if isinstance(change.get("to"), datetime)
                        else change.get("to"),
                    }
                    for key, change in entry.get("changes", {}).items()
                },
            }
            for entry in document.get("history", [])
        ],
    }


def employee_response(document: dict) -> dict:
    """Exclude MongoDB internals and serialize the server timestamp as epoch ms."""
    return {
        "emp_code": document["emp_code"],
        "name": document["name"],
        "email": document["email"],
        "department": document["department"],
        "shift_start": document["shift_start"],
        "shift_end": document["shift_end"],
        "joined_on": document["joined_on"],
        "created_at": datetime_to_epoch_millis(document["created_at"]),
    }


def month_bounds(month: str) -> tuple[str, str, date]:
    """Return the inclusive first day and exclusive first day of a month."""
    year, month_number = (int(part) for part in month.split("-"))
    first_day = date(year, month_number, 1)
    if month_number == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month_number + 1, 1)
    return first_day.isoformat(), next_month.isoformat(), next_month - timedelta(days=1)


def working_days_between(start: date, end: date) -> int:
    """Count inclusive Monday-Friday dates; months bound this loop to 31 days."""
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def rounded_number(value: object, places: int) -> float:
    """Round a MongoDB numeric result with the API's half-up rule."""
    return float(_round_half_up(Decimal(str(value)), places))


def attendance_list_filter(
    emp_code: Optional[str],
    date_from: Optional[date],
    date_to: Optional[date],
    status: Optional[str],
) -> dict:
    """Build the single attendance-list filter used for execution and explain."""
    query: dict = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from.isoformat()
        if date_to is not None:
            query["date"]["$lte"] = date_to.isoformat()
    if status is not None:
        query["status"] = status
    return query


def monthly_employee_pipeline(emp_code: str, month_start: str, next_month: str) -> list[dict]:
    """Aggregate authoritative stored metrics for one employee and month."""
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lt": next_month}}},
        {
            "$set": {
                "_weekday": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                    }
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {"$gte": ["$_weekday", 2]},
                                    {"$lte": ["$_weekday", 6]},
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
        },
    ]


def department_summary_pipeline(
    month_start: str,
    next_month: str,
    month_end: str,
    department: Optional[str],
) -> list[dict]:
    """Aggregate from eligible employees so zero-log employees remain headcount."""
    employee_match: dict = {"joined_on": {"$lte": month_end}}
    if department is not None:
        employee_match["department"] = department
    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lt": ["$date", next_month]},
                                ]
                            }
                        }
                    },
                    {
                        "$set": {
                            "_weekday": {
                                "$dayOfWeek": {
                                    "$dateFromString": {
                                        "dateString": "$date",
                                        "format": "%Y-%m-%d",
                                    }
                                }
                            }
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$set": {
                "_present_logs": {
                    "$filter": {
                        "input": "$logs",
                        "as": "log",
                        "cond": {
                            "$and": [
                                {"$in": ["$$log.status", list(PRESENCE_STATUSES)]},
                                {"$isNumber": "$$log.work_hours"},
                            ]
                        },
                    }
                },
                "_late_logs": {
                    "$filter": {
                        "input": "$logs",
                        "as": "log",
                        "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]},
                    }
                },
            }
        },
        {
            "$set": {
                "_present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$log.status", list(PRESENCE_STATUSES)]},
                                            {"$gte": ["$$log._weekday", 2]},
                                            {"$lte": ["$$log._weekday", 6]},
                                        ]
                                    },
                                    {"$cond": [{"$ifNull": ["$$log.half_day", False]}, 0.5, 1]},
                                    0,
                                ]
                            },
                        }
                    }
                },
                "_work_hours_sum": {"$sum": "$_present_logs.work_hours"},
                "_work_hours_count": {"$size": "$_present_logs"},
                "_late_count": {"$size": "$_late_logs"},
                "_late_minutes": {"$sum": "$_late_logs.late_minutes"},
                "_leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "LEAVE"]},
                        }
                    }
                },
                "_on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$_present_days"},
                "work_hours_sum": {"$sum": "$_work_hours_sum"},
                "work_hours_count": {"$sum": "$_work_hours_count"},
                "late_count": {"$sum": "$_late_count"},
                "total_late_minutes": {"$sum": "$_late_minutes"},
                "leave_count": {"$sum": "$_leave_count"},
                "on_duty_count": {"$sum": "$_on_duty_count"},
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
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$work_hours_sum", "$work_hours_count"]},
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


def late_leaderboard_pipeline(month_start: str, next_month: str, department: Optional[str], limit: int) -> list[dict]:
    """Aggregate late totals, rank ties, and apply the cutoff after ranking."""
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": month_start, "$lt": next_month}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
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


def department_trend_pipeline(
    department: str,
    from_day: str,
    to_day: str,
) -> list[dict]:
    """Gap-fill a daily trend and calculate its seven-row moving average in MongoDB."""
    start_date = date.fromisoformat(from_day)
    end_date_exclusive = date.fromisoformat(to_day) + timedelta(days=1)
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": "$department"}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "day": {
                    "$dateFromString": {
                        "dateString": from_day,
                        "format": "%Y-%m-%d",
                    }
                },
            }
        },
        {
            "$densify": {
                "field": "day",
                "partitionByFields": ["department"],
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.combine(start_date, time.min, tzinfo=UTC),
                        datetime.combine(end_date_exclusive, time.min, tzinfo=UTC),
                    ],
                },
            }
        },
        {"$sort": {"day": 1}},
        {
            "$set": {
                "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d", "timezone": "UTC"}}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day_string": "$date", "department_name": "$department"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", "$$department_name"]},
                                    {"$lte": ["$joined_on", "$$day_string"]},
                                ]
                            }
                        }
                    },
                    {"$count": "headcount"},
                ],
                "as": "_headcount",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day_string": "$date", "department_name": "$department"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$date", "$$day_string"]}}},
                    {
                        "$lookup": {
                            "from": "employees",
                            "let": {"employee_code": "$emp_code"},
                            "pipeline": [
                                {
                                    "$match": {
                                        "$expr": {
                                            "$and": [
                                                {"$eq": ["$emp_code", "$$employee_code"]},
                                                {"$eq": ["$department", "$$department_name"]},
                                            ]
                                        }
                                    }
                                },
                                {"$project": {"_id": 0, "emp_code": 1}},
                            ],
                            "as": "_employee",
                        }
                    },
                    {"$match": {"_employee.0": {"$exists": True}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "_daily",
            }
        },
        {
            "$set": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$_headcount.headcount", 0]}, 0]},
                "present_count": {"$ifNull": [{"$arrayElemAt": ["$_daily.present_count", 0]}, 0]},
                "late_count": {"$ifNull": [{"$arrayElemAt": ["$_daily.late_count", 0]}, 0]},
                "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$day"}, 2]}, {"$lte": [{"$dayOfWeek": "$day"}, 6]}]},
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": 1,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError as exc:
        raise HTTPException(503, "MongoDB is unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = truncate_to_seconds(datetime.now(UTC))
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return employee_response(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    docs = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [employee_response(document) for document in docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = (
        epoch_millis_to_datetime(body.punched_at)
        if body.punched_at is not None
        else truncate_to_seconds(datetime.now(UTC))
    )
    attendance_day = attendance_date_for(ts, emp["shift_start"], emp["shift_end"])
    day_string = attendance_day.isoformat()
    doc = {
        "emp_code": body.emp_code,
        "date": day_string,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return attendance_response(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = (
        epoch_millis_to_datetime(body.punched_at)
        if body.punched_at is not None
        else truncate_to_seconds(datetime.now(UTC))
    )
    candidate = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if candidate is None:
        raise HTTPException(404, "no punch-in found")
    if candidate.get("punch_out") is not None:
        raise HTTPException(409, "attendance record is already punched out")
    punch_in_at = truncate_to_seconds(candidate["punch_in"])
    elapsed_seconds = int((ts - punch_in_at).total_seconds())
    if elapsed_seconds <= 0:
        raise HTTPException(422, "punched_at must be after punch_in")
    if elapsed_seconds > 24 * 60 * 60:
        raise HTTPException(422, "punch-out must be within 24 hours of punch-in")

    work_hours = compute_work_hours(punch_in_at, ts)
    overtime_minutes = compute_overtime(
        ts,
        emp["shift_end"],
        candidate["date"],
        emp["shift_start"],
    )
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": candidate["_id"],
            "punch_in": candidate["punch_in"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": work_hours < 4.50,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record is already punched out")
    return attendance_response(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    q = attendance_list_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    docs = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [attendance_response(document) for document in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: date, body: RegularizeIn):
    """Apply a validated correction and append its audit entry atomically."""
    day_string = date.isoformat()
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": day_string})
    if current is None:
        raise HTTPException(404, "attendance record not found")

    supplied_fields = body.model_fields_set
    requested_time_fields = supplied_fields.intersection({"punch_in", "punch_out"})
    if "status" in supplied_fields and body.status is None:
        raise HTTPException(422, "status cannot be null")
    status = body.status if "status" in supplied_fields else current["status"]
    if status in ("ABSENT", "LEAVE") and requested_time_fields:
        raise HTTPException(422, "ABSENT and LEAVE records cannot include punch times")

    punch_in = current.get("punch_in")
    punch_out = current.get("punch_out")
    if "punch_in" in supplied_fields:
        if body.punch_in is None:
            raise HTTPException(422, "punch_in cannot be null")
        punch_in = epoch_millis_to_datetime(body.punch_in)
    if "punch_out" in supplied_fields:
        if body.punch_out is None:
            raise HTTPException(422, "punch_out cannot be null")
        punch_out = epoch_millis_to_datetime(body.punch_out)

    if status in ("ABSENT", "LEAVE"):
        punch_in = None
        punch_out = None
    elif punch_in is None:
        raise HTTPException(422, "presence status requires a punch_in")

    if punch_in is not None:
        punch_in = truncate_to_seconds(punch_in)
        expected_day = attendance_date_for(
            punch_in,
            employee["shift_start"],
            employee["shift_end"],
        )
        if expected_day.isoformat() != day_string:
            raise HTTPException(422, "punch_in must belong to the record's attendance date")
    if punch_out is not None:
        punch_out = truncate_to_seconds(punch_out)
        elapsed_seconds = int((punch_out - punch_in).total_seconds())
        if elapsed_seconds <= 0:
            raise HTTPException(422, "punch_out must be after punch_in")
        if elapsed_seconds > 24 * 60 * 60:
            raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    if status in ("ABSENT", "LEAVE"):
        late_minutes = 0
        work_hours = None
        overtime_minutes = 0
        half_day = False
    else:
        late_minutes = compute_late_minutes(
            punch_in,
            employee["shift_start"],
            employee["shift_end"],
        )
        if punch_out is None:
            work_hours = None
            overtime_minutes = 0
            half_day = False
        else:
            work_hours = compute_work_hours(punch_in, punch_out)
            overtime_minutes = compute_overtime(
                punch_out,
                employee["shift_end"],
                day_string,
                employee["shift_start"],
            )
            half_day = work_hours < 4.50

    proposed = {
        "status": status,
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }
    previous = {
        "status": current["status"],
        "punch_in": current.get("punch_in"),
        "punch_out": current.get("punch_out"),
        "work_hours": current.get("work_hours"),
        "late_minutes": current.get("late_minutes", 0),
        "overtime_minutes": current.get("overtime_minutes", 0),
        "half_day": current.get("half_day", False),
    }
    changes = {
        field: {"from": previous[field], "to": value}
        for field, value in proposed.items()
        if previous[field] != value
    }
    if not changes:
        raise HTTPException(422, "request does not change the attendance record")

    history_filter = (
        {"history": current["history"]}
        if "history" in current
        else {"history": {"$exists": False}}
    )
    concurrency_filter = {
        "_id": current["_id"],
        "emp_code": emp_code,
        "date": day_string,
        "status": current["status"],
        "punch_in": current.get("punch_in"),
        "punch_out": current.get("punch_out"),
        **history_filter,
    }
    history_entry = {
        "at": truncate_to_seconds(datetime.now(UTC)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    updated = db.attendance_logs.find_one_and_update(
        concurrency_filter,
        {
            "$set": proposed,
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record changed; retry the correction")
    return attendance_response(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
):
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if employee is None:
        raise HTTPException(404, "employee not found")
    try:
        month_start, next_month, month_end = month_bounds(month)
        joined_on = date.fromisoformat(employee["joined_on"])
    except ValueError as exc:
        raise HTTPException(422, "month must be a valid YYYY-MM value") from exc

    summary = next(
        iter(db.attendance_logs.aggregate(monthly_employee_pipeline(emp_code, month_start, next_month))),
        {},
    )
    working_start = max(joined_on, date.fromisoformat(month_start))
    working_days = (
        working_days_between(working_start, month_end)
        if working_start <= month_end
        else 0
    )
    present_days = float(summary.get("present_days", 0))
    attendance_pct = (
        rounded_number(Decimal(str(present_days)) * Decimal(100) / Decimal(working_days), 4)
        if working_days
        else None
    )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": rounded_number(present_days, 2),
        "leave_days": summary.get("leave_days", 0),
        "late_count": summary.get("late_count", 0),
        "total_late_minutes": summary.get("total_late_minutes", 0),
        "total_overtime_minutes": summary.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    department: Optional[str] = None,
):
    try:
        month_start, next_month, month_end = month_bounds(month)
    except ValueError as exc:
        raise HTTPException(422, "month must be a valid YYYY-MM value") from exc
    pipeline = department_summary_pipeline(
        month_start,
        next_month,
        month_end.isoformat(),
        department,
    )
    items = []
    for item in db.employees.aggregate(pipeline):
        average = item.get("avg_work_hours")
        items.append(
            {
                "department": item["department"],
                "headcount": item["headcount"],
                "present_days": rounded_number(item["present_days"], 2),
                "avg_work_hours": rounded_number(average, 2) if average is not None else None,
                "late_count": item["late_count"],
                "total_late_minutes": item["total_late_minutes"],
                "leave_count": item["leave_count"],
                "on_duty_count": item["on_duty_count"],
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    try:
        month_start, next_month, _ = month_bounds(month)
    except ValueError as exc:
        raise HTTPException(422, "month must be a valid YYYY-MM value") from exc
    pipeline = late_leaderboard_pipeline(month_start, next_month, department, limit)
    items = [
        {
            "rank": item["rank"],
            "emp_code": item["emp_code"],
            "name": item["name"],
            "department": item["department"],
            "total_late_minutes": item["total_late_minutes"],
            "late_count": item["late_count"],
        }
        for item in db.attendance_logs.aggregate(pipeline)
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_day: Annotated[date, Query(alias="from")],
    to_day: Annotated[date, Query(alias="to")],
):
    if to_day < from_day:
        raise HTTPException(422, "to must be on or after from")
    if (to_day - from_day).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")

    pipeline = department_trend_pipeline(department, from_day.isoformat(), to_day.isoformat())
    items = []
    for item in db.employees.aggregate(pipeline):
        rate = item.get("attendance_rate")
        moving_average = item.get("moving_avg_7d")
        items.append(
            {
                "date": item["date"],
                "is_working_day": item["is_working_day"],
                "headcount": item["headcount"],
                "present_count": rounded_number(item["present_count"], 2),
                "late_count": item["late_count"],
                "attendance_rate": rounded_number(rate, 4) if rate is not None else None,
                "moving_avg_7d": (
                    rounded_number(moving_average, 4)
                    if moving_average is not None
                    else None
                ),
            }
        )
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}", operation_id="explainEndpoint")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_day: Optional[date] = Query(None, alias="from"),
    to_day: Optional[date] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Return executionStats for the same operation shape used by a live endpoint."""
    if endpoint == "attendance_list":
        if date_from is not None and date_to is not None and date_from > date_to:
            raise HTTPException(422, "date_from must be on or before date_to")
        collection_name = "attendance_logs"
        operation = {
            "find": collection_name,
            "filter": attendance_list_filter(emp_code, date_from, date_to, status),
            "projection": {"_id": 0},
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    elif endpoint == "employee_monthly":
        if not emp_code or month is None:
            raise HTTPException(422, "emp_code and month are required")
        try:
            month_start, next_month, _ = month_bounds(month)
        except ValueError as exc:
            raise HTTPException(422, "month must be a valid YYYY-MM value") from exc
        collection_name = "attendance_logs"
        operation = {
            "aggregate": collection_name,
            "pipeline": monthly_employee_pipeline(emp_code, month_start, next_month),
            "cursor": {},
        }
    elif endpoint == "department_summary":
        if month is None:
            raise HTTPException(422, "month is required")
        try:
            month_start, next_month, month_end = month_bounds(month)
        except ValueError as exc:
            raise HTTPException(422, "month must be a valid YYYY-MM value") from exc
        collection_name = "employees"
        operation = {
            "aggregate": collection_name,
            "pipeline": department_summary_pipeline(
                month_start,
                next_month,
                month_end.isoformat(),
                department,
            ),
            "cursor": {},
        }
    elif endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(422, "month is required")
        try:
            month_start, next_month, _ = month_bounds(month)
        except ValueError as exc:
            raise HTTPException(422, "month must be a valid YYYY-MM value") from exc
        collection_name = "attendance_logs"
        operation = {
            "aggregate": collection_name,
            "pipeline": late_leaderboard_pipeline(
                month_start,
                next_month,
                department,
                limit,
            ),
            "cursor": {},
        }
    else:
        if department is None or from_day is None or to_day is None:
            raise HTTPException(422, "department, from, and to are required")
        if to_day < from_day:
            raise HTTPException(422, "to must be on or after from")
        if (to_day - from_day).days + 1 > 92:
            raise HTTPException(422, "date range cannot exceed 92 days")
        collection_name = "employees"
        operation = {
            "aggregate": collection_name,
            "pipeline": department_trend_pipeline(
                department,
                from_day.isoformat(),
                to_day.isoformat(),
            ),
            "cursor": {},
        }

    explain = db.command(
        "explain",
        operation,
        verbosity="executionStats",
    )
    serialized_explain = json.loads(json_util.dumps(explain))
    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": serialized_explain,
    }

