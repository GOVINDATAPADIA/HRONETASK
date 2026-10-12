"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

# Environment takes priority over defaults
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Lifespan & Index Creation
# --------------------------------------------------------------------------- #
def ensure_indexes():
    """Create all required indexes idempotently at startup."""
    # employees collection
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])

    # attendance_logs collection
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("status", 1), ("date", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers & Rules (R1 - R8)
# --------------------------------------------------------------------------- #
def truncate_millis(epoch_ms: int) -> int:
    """Truncate epoch milliseconds to whole seconds (R1)."""
    return (epoch_ms // 1000) * 1000


def epoch_to_datetime_utc(epoch_ms: int) -> datetime:
    """Convert epoch milliseconds to timezone-aware UTC datetime."""
    sec = truncate_millis(epoch_ms) / 1000.0
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime (naive UTC or aware) to epoch milliseconds integer."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    """Perform half-up rounding (R8)."""
    if val is None:
        return None
    fmt = "0." + "0" * decimals
    return float(Decimal(str(val)).quantize(Decimal(fmt), rounding=ROUND_HALF_UP))


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """Check if a shift crosses midnight (shift_end <= shift_start)."""
    sh_h, sh_m = map(int, shift_start.split(":"))
    eh_h, eh_m = map(int, shift_end.split(":"))
    return (eh_h, eh_m) <= (sh_h, sh_m)


def get_attendance_date(punch_in_ist: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: A record's date is the IST calendar date of its punch_in instant.
    Exception - overnight shifts (shift_end <= shift_start): a punch-in whose
    IST time is earlier than shift_end belongs to the shift that started the previous day.
    """
    eh_h, eh_m = map(int, shift_end.split(":"))
    if is_overnight_shift(shift_start, shift_end):
        if (punch_in_ist.hour, punch_in_ist.minute) < (eh_h, eh_m):
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_ist: datetime, shift_start: str, attendance_date_str: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if punch_in is strictly more than 10:00
    after shift_start; then late_minutes = whole minutes (floor) since shift_start.
    """
    sh_h, sh_m = map(int, shift_start.split(":"))
    att_date = date.fromisoformat(attendance_date_str)
    start_dt = datetime.combine(att_date, time(sh_h, sh_m), tzinfo=IST)
    diff_sec = (punch_in_ist - start_dt).total_seconds()
    if diff_sec > 600:  # strictly more than 10 minutes (600 seconds)
        return int(diff_sec // 60)
    return 0


def compute_overtime(
    punch_out_ist: datetime, shift_start: str, shift_end: str, attendance_date_str: str
) -> int:
    """
    R3: Overtime. Whole minutes (floor) from shift_end to punch_out,
    counted only if that is at least 30; otherwise 0.
    """
    sh_h, sh_m = map(int, shift_start.split(":"))
    eh_h, eh_m = map(int, shift_end.split(":"))
    att_date = date.fromisoformat(attendance_date_str)
    if is_overnight_shift(shift_start, shift_end):
        end_date = att_date + timedelta(days=1)
    else:
        end_date = att_date
    end_dt = datetime.combine(end_date, time(eh_h, eh_m), tzinfo=IST)
    diff_sec = (punch_out_ist - end_dt).total_seconds()
    if diff_sec >= 1800:  # >= 30 minutes
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    return round_half_up(diff_sec / 3600.0, 2)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: half_day is true when the rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def count_working_days(joined_on_str: str, month_str: str) -> int:
    """
    R7: Working days Monday-Friday in the month, starting from joined_on for mid-month joiners.
    """
    year, month = map(int, month_str.split("-"))
    start_date = date(year, month, 1)
    if month == 12:
        end_date = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        end_date = date(year, month + 1, 1) - timedelta(days=1)

    joined_date = date.fromisoformat(joined_on_str)
    if joined_date > end_date:
        return 0
    if joined_date > start_date:
        start_date = joined_date

    count = 0
    cur = start_date
    while cur <= end_date:
        if cur.weekday() < 5:  # 0 to 4 are Mon to Fri
            count += 1
        cur += timedelta(days=1)
    return count


def get_month_range(month_str: str) -> tuple[str, str]:
    """Return (start_date_str, end_date_str) for YYYY-MM."""
    year, month = map(int, month_str.split("-"))
    start_date = date(year, month, 1)
    if month == 12:
        end_date = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        end_date = date(year, month + 1, 1) - timedelta(days=1)
    return start_date.isoformat(), end_date.isoformat()


# --------------------------------------------------------------------------- #
# Serialization Helpers
# --------------------------------------------------------------------------- #
def serialize_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    doc.pop("_id", None)
    if "created_at" in doc and isinstance(doc["created_at"], datetime):
        doc["created_at"] = datetime_to_epoch_ms(doc["created_at"])
    return doc


def serialize_history_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    at_val = entry.get("at")
    if isinstance(at_val, datetime):
        at_ms = datetime_to_epoch_ms(at_val)
    else:
        at_ms = at_val

    changes_out = {}
    for k, v in entry.get("changes", {}).items():
        from_val = v.get("from")
        to_val = v.get("to")
        if isinstance(from_val, datetime):
            from_val = datetime_to_epoch_ms(from_val)
        if isinstance(to_val, datetime):
            to_val = datetime_to_epoch_ms(to_val)
        changes_out[k] = {"from": from_val, "to": to_val}

    return {
        "at": at_ms,
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes_out,
    }


def serialize_attendance_record(doc: Dict[str, Any]) -> Dict[str, Any]:
    doc.pop("_id", None)
    doc.pop("id", None)
    punch_in = doc.get("punch_in")
    punch_out = doc.get("punch_out")

    raw_history = doc.get("history") or []
    history_serialized = [serialize_history_entry(h) for h in raw_history]

    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": datetime_to_epoch_ms(punch_in) if isinstance(punch_in, datetime) else punch_in,
        "punch_out": datetime_to_epoch_ms(punch_out) if isinstance(punch_out, datetime) else punch_out,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": history_serialized,
    }


# --------------------------------------------------------------------------- #
# Pydantic Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    status: str = Field(default="PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(default=None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB is unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    now_ms = truncate_millis(int(datetime.now(timezone.utc).timestamp() * 1000))
    now_dt = epoch_to_datetime_utc(now_ms)
    doc = body.model_dump()
    doc["created_at"] = now_dt
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Unknown employee")

    if body.punched_at is not None:
        p_ms = truncate_millis(body.punched_at)
    else:
        p_ms = truncate_millis(int(datetime.now(timezone.utc).timestamp() * 1000))

    punch_in_dt = epoch_to_datetime_utc(p_ms)
    punch_in_ist = punch_in_dt.astimezone(IST)
    att_date = get_attendance_date(punch_in_ist, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(punch_in_ist, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return serialize_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Unknown employee")

    if body.punched_at is not None:
        p_ms = truncate_millis(body.punched_at)
    else:
        p_ms = truncate_millis(int(datetime.now(timezone.utc).timestamp() * 1000))

    punch_out_dt = epoch_to_datetime_utc(p_ms)

    # Find employee's most recent record whose punch_in <= punched_at
    latest_record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )

    if not latest_record:
        raise HTTPException(status_code=404, detail="No matching punch-in found")

    if latest_record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    punch_in_dt = latest_record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    # Validation: punched_at <= punch_in or > 24 hours after punch_in
    if punch_out_dt <= punch_in_dt or punch_out_dt > punch_in_dt + timedelta(hours=24):
        raise HTTPException(status_code=422, detail="Invalid punch_out time")

    punch_out_ist = punch_out_dt.astimezone(IST)
    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    half_day = compute_half_day(work_hours)
    overtime_min = compute_overtime(
        punch_out_ist, emp["shift_start"], emp["shift_end"], latest_record["date"]
    )

    # Atomic update to guarantee single 200 under concurrency
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": latest_record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_min,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Record was punched out concurrently")

    return serialize_attendance_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    q: Dict[str, Any] = {}
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
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance_record(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    # Determine final status and punch times
    old_status = record.get("status")
    new_status = body.status if body.status is not None else old_status

    # Rule: setting status ABSENT or LEAVE clears both punch times; supplying punch times is 422
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422, detail="Punch times cannot be supplied with ABSENT or LEAVE status"
            )
        final_punch_in_dt = None
        final_punch_out_dt = None
    else:
        # Presence status
        if body.punch_in is not None:
            final_punch_in_dt = epoch_to_datetime_utc(body.punch_in)
        else:
            final_punch_in_dt = record.get("punch_in")

        if final_punch_in_dt is None:
            raise HTTPException(
                status_code=422, detail="Presence status requires a punch_in"
            )

        if body.punch_out is not None:
            final_punch_out_dt = epoch_to_datetime_utc(body.punch_out)
        else:
            final_punch_out_dt = record.get("punch_out")

    # Ensure tzinfo is set on datetimes
    if final_punch_in_dt and final_punch_in_dt.tzinfo is None:
        final_punch_in_dt = final_punch_in_dt.replace(tzinfo=timezone.utc)
    if final_punch_out_dt and final_punch_out_dt.tzinfo is None:
        final_punch_out_dt = final_punch_out_dt.replace(tzinfo=timezone.utc)

    # Validate punch_in stays on the record's attendance date per R1
    if final_punch_in_dt is not None:
        p_in_ist = final_punch_in_dt.astimezone(IST)
        computed_date = get_attendance_date(p_in_ist, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(
                status_code=422, detail="punch_in must stay on the record's attendance date"
            )

    # Validate punch_out constraints
    if final_punch_out_dt is not None:
        if final_punch_in_dt is None or final_punch_out_dt <= final_punch_in_dt or final_punch_out_dt > final_punch_in_dt + timedelta(hours=24):
            raise HTTPException(
                status_code=422, detail="punch_out must be after punch_in and within 24 hours"
            )

    # Recompute derived fields
    if new_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(
            final_punch_in_dt.astimezone(IST), emp["shift_start"], date
        )
        if final_punch_out_dt is not None:
            new_work_hours = compute_work_hours(final_punch_in_dt, final_punch_out_dt)
            new_half_day = compute_half_day(new_work_hours)
            new_overtime_minutes = compute_overtime(
                final_punch_out_dt.astimezone(IST),
                emp["shift_start"],
                emp["shift_end"],
                date,
            )
        else:
            new_work_hours = None
            new_half_day = False
            new_overtime_minutes = 0

    # Build changes map
    old_punch_in = record.get("punch_in")
    if old_punch_in and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)
    old_punch_out = record.get("punch_out")
    if old_punch_out and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)

    changes: Dict[str, Dict[str, Any]] = {}
    if old_status != new_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if old_punch_in != final_punch_in_dt:
        changes["punch_in"] = {"from": old_punch_in, "to": final_punch_in_dt}
    if old_punch_out != final_punch_out_dt:
        changes["punch_out"] = {"from": old_punch_out, "to": final_punch_out_dt}
    if record.get("work_hours") != new_work_hours:
        changes["work_hours"] = {"from": record.get("work_hours"), "to": new_work_hours}
    if record.get("late_minutes", 0) != new_late_minutes:
        changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": new_late_minutes}
    if record.get("overtime_minutes", 0) != new_overtime_minutes:
        changes["overtime_minutes"] = {
            "from": record.get("overtime_minutes", 0),
            "to": new_overtime_minutes,
        }
    if bool(record.get("half_day", False)) != new_half_day:
        changes["half_day"] = {"from": bool(record.get("half_day", False)), "to": new_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="Request does not change any fields")

    now_dt = epoch_to_datetime_utc(int(datetime.now(timezone.utc).timestamp() * 1000))
    history_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic update checking history length to prevent lost updates
    existing_history = record.get("history") or []
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": {"$size": len(existing_history)},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": final_punch_in_dt,
                "punch_out": final_punch_out_dt,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(
            status_code=409, detail="Record was modified concurrently; please retry"
        )

    return serialize_attendance_record(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    m_start, m_end = get_month_range(month)
    working_days = count_working_days(emp["joined_on"], month)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": m_start, "$lte": m_end},
            }
        },
        {
            "$addFields": {
                "day_of_week": {
                    "$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}
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
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$day_of_week", 1]},  # Not Sunday
                                    {"$ne": ["$day_of_week", 7]},  # Not Saturday
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    agg_results = list(db.attendance_logs.aggregate(pipeline))
    if agg_results:
        res = agg_results[0]
        present_days = float(res.get("present_days", 0.0))
        leave_days = int(res.get("leave_days", 0))
        late_count = int(res.get("late_count", 0))
        total_late = int(res.get("total_late_minutes", 0))
        total_ot = int(res.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100.0, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary_analytics(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    m_start, m_end = get_month_range(month)

    emp_match: Dict[str, Any] = {"joined_on": {"$lte": m_end}}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emps": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$emps"]},
                                    {"$gte": ["$date", m_start]},
                                    {"$lte": ["$date", m_end]},
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "day_of_week": {
                                "$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                            }
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$l.day_of_week", 1]},
                                            {"$ne": ["$$l.day_of_week", 7]},
                                        ]
                                    },
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "avg_work_hours": {
                    "$avg": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$l.work_hours", None]},
                                        ]
                                    },
                                }
                            },
                            "as": "l",
                            "in": "$$l.work_hours",
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]},
                        }
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]},
                        }
                    }
                },
            }
        },
        {"$sort": {"department": 1}},
    ]

    items = []
    for row in db.employees.aggregate(pipeline):
        items.append({
            "department": row["department"],
            "headcount": int(row["headcount"]),
            "present_days": float(row["present_days"]),
            "avg_work_hours": round_half_up(row.get("avg_work_hours"), 2),
            "late_count": int(row["late_count"]),
            "total_late_minutes": int(row["total_late_minutes"]),
            "leave_count": int(row["leave_count"]),
            "on_duty_count": int(row["on_duty_count"]),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def leaderboard_late_analytics(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    m_start, m_end = get_month_range(month)

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": m_start, "$lte": m_end},
                "late_minutes": {"$gt": 0},
            }
        },
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
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend_analytics(
    department: str,
    date_from: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    # Verify department exists
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=404, detail="Unknown department")

    d_from = date.fromisoformat(date_from)
    d_to = date.fromisoformat(date_to)

    if d_to < d_from or (d_to - d_from).days > 92:
        raise HTTPException(status_code=422, detail="Invalid date range (max 92 days)")

    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$project": {
                "day_offset": {
                    "$range": [
                        0,
                        {
                            "$add": [
                                {
                                    "$dateDiff": {
                                        "startDate": {"$dateFromString": {"dateString": date_from}},
                                        "endDate": {"$dateFromString": {"dateString": date_to}},
                                        "unit": "day",
                                    }
                                },
                                1,
                            ]
                        },
                    ]
                }
            }
        },
        {"$unwind": "$day_offset"},
        {
            "$project": {
                "date_dt": {
                    "$dateAdd": {
                        "startDate": {"$dateFromString": {"dateString": date_from}},
                        "unit": "day",
                        "amount": "$day_offset",
                    }
                }
            }
        },
        {
            "$project": {
                "date": {
                    "$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}
                },
                "day_of_week": {"$dayOfWeek": "$date_dt"},
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$not": {"$in": ["$day_of_week", [1, 7]]}}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$d"]},
                                ]
                            }
                        }
                    },
                    {"$project": {"_id": 0, "emp_code": 1}},
                ],
                "as": "dept_emps",
            }
        },
        {"$addFields": {"headcount": {"$size": "$dept_emps"}}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"d": "$date", "codes": "$dept_emps.emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$d"]},
                                    {"$in": ["$emp_code", "$$codes"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]},
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {"$sort": {"date": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
    ]

    items = []
    for row in db.employees.aggregate(pipeline):
        items.append({
            "date": row["date"],
            "is_working_day": bool(row["is_working_day"]),
            "headcount": int(row["headcount"]),
            "present_count": float(row["present_count"]),
            "late_count": int(row["late_count"]),
            "attendance_rate": round_half_up(row.get("attendance_rate"), 4),
            "moving_avg_7d": round_half_up(row.get("moving_avg_7d"), 4),
        })

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    valid_endpoints = [
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ]
    if endpoint not in valid_endpoints:
        raise HTTPException(status_code=422, detail="Invalid endpoint name")

    if endpoint == "attendance_list":
        q: Dict[str, Any] = {}
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
        skip = (page - 1) * page_size
        cursor = (
            db.attendance_logs.find(q)
            .sort([("date", -1), ("emp_code", 1)])
            .skip(skip)
            .limit(page_size)
        )
        exp = cursor.explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        m_start, m_end = get_month_range(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": m_start, "$lte": m_end}}},
            {"$addFields": {"day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$ne": ["$day_of_week", 1]},
                                        {"$ne": ["$day_of_week", 7]},
                                    ]
                                },
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                }
            },
        ]
        exp = db.command({
            "explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        m_start, m_end = get_month_range(month)
        emp_match: Dict[str, Any] = {"joined_on": {"$lte": m_end}}
        if department:
            emp_match["department"] = department
        pipeline = [
            {"$match": emp_match},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "emp_codes": {"$push": "$emp_code"}}},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emps": "$emp_codes"},
                    "pipeline": [
                        {"$match": {"$expr": {"$and": [{"$in": ["$emp_code", "$$emps"]}, {"$gte": ["$date", m_start]}, {"$lte": ["$date", m_end]}]}}},
                    ],
                    "as": "logs",
                }
            },
        ]
        exp = db.command({
            "explain": {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "employees", "explain": exp}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        m_start, m_end = get_month_range(month)
        pipeline = [
            {"$match": {"date": {"$gte": m_start, "$lte": m_end}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
            {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"},
            {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
            {"$match": {"rank": {"$lte": limit}}},
        ]
        exp = db.command({
            "explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}

    elif endpoint == "department_trend":
        target_dept = department
        start_d = from_date
        end_d = to_date
        if not target_dept or not start_d or not end_d:
            raise HTTPException(
                status_code=422, detail="department, from, and to are required for department_trend"
            )
        pipeline = [
            {"$match": {"department": target_dept}},
            {"$limit": 1},
        ]
        exp = db.command({
            "explain": {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "employees", "explain": exp}
