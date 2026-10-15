"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""

import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP    #Decimal with ROUND_HALF_UP will implement the rounding rules
from typing import Optional, Literal, Annotated

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError    #DuplicateKeyError will handle uniqueness conflicts safely.

load_dotenv()

UTC = timezone.utc    #UTC (Coordinated Universal Time)
IST = timezone(timedelta(hours=5, minutes=30))    #represents India's timezone, which is UTC + 5 hours 30 minutes.

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True, 
    tzinfo=UTC,    #tz_aware=True and tzinfo=UTC make MongoDB datetimes timezone-aware when read.
    serverSelectionTimeoutMS=3000,    #wait time to find a suitable MongoDB server
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
)

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = (*PRESENCE_STATUSES, "ABSENT", "LEAVE")
EPOCH_MIN_MS = 100_000_000_000    #approx date = September 1973, numeric boundaries for Unix epoch timestamps in milliseconds. A Unix timestamp represents time as the number of milliseconds or seconds elapsed since January 1, 1970, at 00:00:00 UTC.
EPOCH_MAX_MS = 4_102_444_800_000    #approx = 4_102_444_800_000	January 1, 2100. values are in milliseconds
#why did I use epoch? for the API can check whether a timestamp falls within an acceptable range before processing it.


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def truncate_seconds(value: datetime) -> datetime:
    """Truncate an instant to whole seconds."""
    return value.replace(microsecond=0)


def epoch_ms_to_datetime(value: int) -> datetime:
    """Validate an epoch-millisecond timestamp and return UTC datetime."""
    if type(value) is not int or not EPOCH_MIN_MS <= value <= EPOCH_MAX_MS:
        raise ValueError("Timestamp must be a valid epoch-millisecond integer.")

    seconds, milliseconds = divmod(value, 1000)
    return datetime.fromtimestamp(seconds, tz=UTC).replace(
        microsecond=milliseconds * 1000
    )


def datetime_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    """Convert a stored datetime to epoch milliseconds."""
    if value is None:
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)

    return int(value.timestamp() * 1000)


def round_half_up(value: float, places: int = 2) -> float:    #for rounding up
    quantum = Decimal("1").scaleb(-places)
    result = Decimal(str(value)).quantize(
        quantum, rounding=ROUND_HALF_UP
    )
    return float(result)


def shift_start_datetime(attendance_date: str, shift_start: str) -> datetime:
    day = date.fromisoformat(attendance_date)
    hour, minute = map(int, shift_start.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=IST)
'''
this makes the shift start independent, in the previous code the shift start was created ny modiying punch-in datetime
def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    minutes = int((punch_in - start).total_seconds() / 60)
    return minutes if minutes > 10 else 0
'''


def shift_end_datetime(
    attendance_date: str,
    shift_start: str,
    shift_end: str,
) -> datetime:
    day = date.fromisoformat(attendance_date)
    start_hour, start_minute = map(int, shift_start.split(":"))
    end_hour, end_minute = map(int, shift_end.split(":"))

    start_minutes = start_hour * 60 + start_minute
    end_minutes = end_hour * 60 + end_minute

    end_day = day + timedelta(days=1) if end_minutes <= start_minutes else day    # it adds + 1day, if start time=22:00/1320mins and end time=06:00/360 mind it implies that event is ended the next day

    return datetime.combine(
        end_day,
        time(end_hour, end_minute),
        tzinfo=IST,
    )


def attendance_date_for(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> str:    #prevents an overnight punch-in at, for example, 02:00 IST from being incorrectly recorded as a new attendance day for a 22:00–06:00 shift.
    """Return the shift-start date, including overnight shifts."""
    local = punch_in.astimezone(IST)    #converts the local timezone into IST
    end_hour, end_minute = map(int, shift_end.split(":"))
    start_hour, start_minute = map(int, shift_start.split(":"))

    start_minutes = start_hour * 60 + start_minute
    end_minutes = end_hour * 60 + end_minute
    local_minutes = local.hour * 60 + local.minute

    day = local.date()

    if end_minutes <= start_minutes and local_minutes < end_minutes:
        day -= timedelta(days=1)

    return day.isoformat()


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    attendance_date: str,
) -> int:
    punch_in = truncate_seconds(punch_in)
    start = shift_start_datetime(attendance_date, shift_start)

    elapsed_seconds = (punch_in - start).total_seconds()

    # Exactly ten minutes late is still within the grace period.
    if elapsed_seconds <= 600:
        return 0

    return max(0, int(elapsed_seconds // 60))


def compute_work_hours(
    punch_in: datetime,
    punch_out: datetime,
) -> float:
    seconds = (
        truncate_seconds(punch_out) - truncate_seconds(punch_in)
    ).total_seconds()

    return round_half_up(seconds / 3600, 2)


def compute_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    attendance_date: str,
) -> int:
    punch_out = truncate_seconds(punch_out)
    end = shift_end_datetime(attendance_date, shift_start, shift_end)

    elapsed_seconds = (punch_out - end).total_seconds()
    minutes = int(elapsed_seconds // 60)

    return minutes if minutes >= 30 else 0


def compute_derived_fields(
    status: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    shift_start: str,
    shift_end: str,
    attendance_date: str,
) -> dict:    #gives one consistent place to recalculate all derived values after a manual correction.
    """Compute all derived values consistently after a write."""
    if status not in PRESENCE_STATUSES:
        return {
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }

    late = (
        compute_late_minutes(punch_in, shift_start, attendance_date)
        if punch_in is not None else 0
    )

    if punch_in is None or punch_out is None:
        return {
            "work_hours": None,
            "late_minutes": late,
            "overtime_minutes": 0,
            "half_day": False,
        }

    hours = compute_work_hours(punch_in, punch_out)
    overtime = compute_overtime(
        punch_out, shift_start, shift_end, attendance_date
    )

    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": hours < 4.50,
    }



# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

EpochMillis = Annotated[
    int,
    Field(strict=True, ge=EPOCH_MIN_MS, le=EPOCH_MAX_MS),
]


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(
        default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )
    shift_end: str = Field(
        default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )
    joined_on: date

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[Literal[
        "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
    ]] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)



# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now()
    db.employees.insert_one(doc)
    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    q = {}
    if department:
        q["department"] = department
    skip = page * page_size
    total = db.employees.count_documents({})
    items = list(db.employees.find(q, {"_id": 0}).skip(skip).limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    ts = datetime.fromtimestamp(body.punched_at / 1000) if body.punched_at else datetime.now()
    d = ts.date().isoformat()
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    res = db.attendance_logs.insert_one(doc)
    doc["id"] = str(res.inserted_id)
    doc.pop("_id", None)
    return doc


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    docs = list(db.attendance_logs.find(q))
    docs.sort(key=lambda d: d["date"], reverse=True)
    total = len(docs)
    page_docs = docs[(page - 1) * page_size : page * page_size]
    for d in page_docs:
        d["id"] = str(d.pop("_id"))
    return {"items": page_docs, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
