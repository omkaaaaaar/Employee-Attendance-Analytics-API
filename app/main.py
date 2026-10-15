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
from pydantic import BaseModel, ConfigDict, Field, model_validator
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

@app.on_event("startup")
def create_indexes():
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employee_emp_code",
    )

    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employee_department_code",
    )

    db.employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING)],
        name="ix_employee_joined_department",
    )

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )

    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_employee",
    )

    db.attendance_logs.create_index(
        [
            ("emp_code", ASCENDING),
            ("date", ASCENDING),
            ("status", ASCENDING),
        ],
        name="ix_attendance_employee_date_status",
    )

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="ix_attendance_open_punch",
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



def serialize_document(doc: dict) -> dict:
    def convert(value):
        if isinstance(value, datetime):
            return datetime_to_epoch_ms(value)
        if isinstance(value, list):
            return [convert(item) for item in value]
        if isinstance(value, dict):
            return {
                key: convert(item)
                for key, item in value.items()
                if key != "_id"
            }
        return value

    return convert({k: v for k, v in doc.items() if k != "_id"})



# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

EpochMillis = Annotated[
    int,
    Field(strict=True, ge=EPOCH_MIN_MS, le=EPOCH_MAX_MS),
]


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")    #Pydantic to ignore fields that aren't defined in the model.

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")    #^EMP: must start with EMP, \d{4, 6}: must contain 4 to 6 digits, $: must end there
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",    #Some characters before @, An @ symbol, A domain containing a dot, No spaces
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
    try:
        db.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        )




@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump(mode="json")
    doc["created_at"] = datetime.now(UTC)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return serialize_document(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if department is not None:
        query["department"] = department

    total = db.employees.count_documents(query)

    cursor = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [serialize_document(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }



@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    try:
        ts = (
            epoch_ms_to_datetime(body.punched_at)
            if body.punched_at is not None
            else datetime.now(UTC)
        )
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(422, "invalid punched_at timestamp")

    d = attendance_date_for(ts, emp["shift_start"], emp["shift_end"])
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return serialize_document(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
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

    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort("date", DESCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    page_docs = [serialize_document(d) for d in cursor]
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
