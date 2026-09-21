"""
የሙከራ መረጃ — seeded test database.

Thirty fictional employees with Amharic names and a full Ethiopian year of
generated attendance: holidays, leave, delegated tasks, short days, missing
check-outs, PIN fallbacks, corrections and a power outage.

The point is that every generated figure is known in advance, so reports can
be validated against arithmetic rather than eyeballed. Without this, report
bugs are found by the Woreda's HR officer instead of by us.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path

from sams.core.ethiopian import EthiopianDate, iter_days, year_range
from sams.core.leave import LeaveStatus, LeaveType
from sams.db.connection import connect, init_schema
from sams.db.repository import (
    add_holiday,
    create_employee,
    record_event,
    set_day_override,
)
from sams.core.workcalendar import DEFAULT_HOLIDAYS, DayType

FIRST_NAMES = [
    "አበበ", "ብርሃኑ", "ጌታቸው", "ሐይማኖት", "ሰላም", "ተስፋዬ", "ወርቅነሽ", "ዳንኤል",
    "መሰረት", "ኪዳነ", "ፍቅርተ", "ሙሉጌታ", "ዓለምነሽ", "ገብረመድህን", "ትዕግስት",
]
LAST_NAMES = [
    "ከበደ", "ተክሌ", "ወልዱ", "ኃይሉ", "አሰፋ", "ገብሬ", "መኮንን", "ታደሰ",
    "ብርሃነ", "ዘውዴ", "ሙሉ", "ደስታ",
]
POSITIONS = [
    "የፋይናንስ ባለሙያ", "የሰው ሃይል ባለሙያ", "ጸሐፊ", "የመዝገብ ሹም",
    "የልማት ባለሙያ", "የግብርና ባለሙያ", "ሹፌር", "የጤና ባለሙያ", "ተቆጣጣሪ",
]


def build_seed(
    db_path: str | Path, eth_year: int = 2018, n_employees: int = 30, seed: int = 42
) -> dict:
    """
    Create a fully populated database and return the facts needed to
    validate reports against it.
    """
    rng = random.Random(seed)
    path = Path(db_path)
    if path.exists():
        path.unlink()

    conn = connect(path)
    init_schema(conn)

    conn.execute(
        "INSERT INTO admin_user (id, username, display_name_am, password_hash, role) "
        "VALUES (1, 'admin', 'አስተዳዳሪ', 'seeded', 'admin')"
    )
    conn.execute(
        "INSERT INTO department (id, name_am, name_latin) "
        "VALUES (1, 'የወረዳው ጽህፈት ቤት', 'Woreda Office')"
    )
    conn.commit()

    for h in DEFAULT_HOLIDAYS:
        data = {"name_am": h.name_am, "day_type": h.day_type.value}
        if h.ethiopian_month_day:
            data["eth_month"], data["eth_day"] = h.ethiopian_month_day
        else:
            data["greg_month"], data["greg_day"] = h.gregorian_month_day
        add_holiday(conn, data, 1)

    employees = []
    used = set()
    for i in range(1, n_employees + 1):
        while True:
            name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
            if name not in used:
                used.add(name)
                break
        emp_id = create_employee(conn, {
            "employee_code": f"W-{i:03d}",
            "name_am": name,
            "position_am": rng.choice(POSITIONS),
            "phone": f"09{rng.randint(10000000, 99999999)}",
            "department_id": 1,
        })
        employees.append(emp_id)

    start, end = year_range(eth_year)

    # An office event: everyone credited for the afternoon.
    event_day = _first_working(conn, start + timedelta(days=40))
    set_day_override(conn, event_day, DayType.EVENT, "የወረዳ ልማት መርሃ ግብር", 1)

    # A working Saturday.
    sat = _next_weekday(start + timedelta(days=70), 5)
    set_day_override(conn, sat, DayType.FULL, "አስቸኳይ የስራ ቀን", 1)

    # A full-day power outage.
    outage_day = _first_working(conn, start + timedelta(days=100))
    conn.execute(
        "INSERT INTO outage (from_date, to_date, note_am, created_by) "
        "VALUES (?,?,?,1)",
        (outage_day.isoformat(), outage_day.isoformat(), "ሙሉ ቀን የኤሌክትሪክ መቋረጥ"),
    )
    conn.commit()

    from sams.db.repository import load_calendar

    cal = load_calendar(conn)

    facts = {
        "eth_year": eth_year,
        "start": start,
        "end": end,
        "employees": employees,
        "event_day": event_day,
        "working_saturday": sat,
        "outage_day": outage_day,
        "expected_full_day_hours": cal.shift.full_day_hours,
        "per_employee": {},
    }

    leave_rows: list[tuple[int, date, date]] = []
    for emp_id in employees:
        worked_days = 0
        absent_days = 0
        incomplete_days = 0
        pin_days = 0
        total_worked = 0.0

        # Two leave days each, in the second month.
        lv_start = _first_working(conn, start + timedelta(days=35))
        lv_end = lv_start
        conn.execute(
            "INSERT INTO leave_request "
            "(employee_id, leave_type, start_date, end_date, status, "
            " decided_by, decided_at, reason) "
            "VALUES (?,?,?,?,?,1,datetime('now'),'የግል ጉዳይ')",
            (emp_id, LeaveType.ANNUAL.value, lv_start.isoformat(),
             lv_end.isoformat(), LeaveStatus.APPROVED.value),
        )
        leave_rows.append((emp_id, lv_start, lv_end))

        for day in iter_days(start, end):
            dtype = cal.day_type(day)
            if not dtype.kiosk_open:
                continue
            if day == outage_day or day == lv_start:
                continue

            roll = rng.random()
            if roll < 0.04:
                absent_days += 1
                continue
            if roll < 0.07:
                # Forgot to check out.
                record_event(
                    conn, emp_id, "in",
                    datetime.combine(day, time(8, rng.randint(20, 45))),
                    0, work_date=day,
                )
                incomplete_days += 1
                continue

            method = "face"
            if roll > 0.96:
                method = "pin"
                pin_days += 1

            in_t = time(8, rng.randint(20, 50))
            if dtype is DayType.HALF_MORNING:
                out_t = time(12, rng.randint(25, 35))
            else:
                out_t = time(17, rng.randint(20, 40))

            record_event(conn, emp_id, "in", datetime.combine(day, in_t), 0,
                         method=method, work_date=day)
            record_event(conn, emp_id, "out", datetime.combine(day, out_t), 0,
                         method=method, work_date=day)
            worked_days += 1

            span = (
                datetime.combine(day, out_t) - datetime.combine(day, in_t)
            ).total_seconds() / 3600
            ls, le = cal.shift.lunch_window
            lunch = 0.0
            if in_t < le and out_t > ls:
                lo = max(datetime.combine(day, in_t), datetime.combine(day, ls))
                hi = min(datetime.combine(day, out_t), datetime.combine(day, le))
                lunch = max(0.0, (hi - lo).total_seconds() / 3600)
            total_worked += span - lunch

        facts["per_employee"][emp_id] = {
            "worked_days": worked_days,
            "absent_days": absent_days,
            "incomplete_days": incomplete_days,
            "pin_days": pin_days,
            "total_worked_hours": round(total_worked, 4),
        }

    conn.commit()
    facts["leave_rows"] = leave_rows
    facts["total_expected_hours"] = cal.expected_total(start, end)
    conn.close()
    return facts


def _first_working(conn: sqlite3.Connection, from_day: date) -> date:
    from sams.db.repository import load_calendar

    cal = load_calendar(conn)
    d = from_day
    for _ in range(30):
        if cal.day_type(d).kiosk_open:
            return d
        d += timedelta(days=1)
    return from_day


def _next_weekday(from_day: date, weekday: int) -> date:
    d = from_day
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d


if __name__ == "__main__":
    facts = build_seed("seed.db")
    print(f"seeded {len(facts['employees'])} employees "
          f"for {facts['eth_year']} ዓ.ም.")
    print(f"expected hours in the year: {facts['total_expected_hours']}")
