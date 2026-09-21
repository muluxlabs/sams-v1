"""
የሪፖርት ግንባታ — report data builders.

Reports are what the Administration actually buys. Nobody outside the HR
office will ever see the recognition code; everyone sees the monthly sheet.

Two rules:
  * Every figure is recomputed from raw events at generation time. Nothing
    is cached, so a correction made this morning shows in a report run this
    afternoon without anyone rebuilding anything.
  * Every total expands to the days behind it, and every day to its punches.
    Attendance feeds payroll here, so a figure nobody can explain is a
    figure that cannot be defended in a dispute.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..core.ethiopian import EthiopianDate, month_range, year_range
from ..core.hours import DayStatus, HoursEngine
from ..core.leave import LeaveService, LeaveStatus
from ..db import repository as repo


@dataclass
class ReportContext:
    """Everything a report needs, loaded once."""

    conn: sqlite3.Connection
    start: date
    end: date
    title_am: str

    @property
    def period_label_am(self) -> str:
        a = EthiopianDate.from_gregorian(self.start)
        b = EthiopianDate.from_gregorian(self.end)
        if (a.year, a.month) == (b.year, b.month):
            return f"{a.month_name_am} {a.year} ዓ.ም."
        return f"{a.format_am()} — {b.format_am()}"


def _engine(conn: sqlite3.Connection) -> HoursEngine:
    return HoursEngine(repo.load_calendar(conn))


def _leave_map(
    conn: sqlite3.Connection, employee_id: int, start: date, end: date
) -> dict[date, bool]:
    cal = repo.load_calendar(conn)
    svc = LeaveService(cal)
    requests = [
        r for r in repo.list_leave(
            conn, employee_id, LeaveStatus.APPROVED, overlapping=(start, end)
        )
    ]
    return svc.leave_days_for_period(requests, start, end)


# -- 1. daily register ---------------------------------------------------


def daily_register(conn: sqlite3.Connection, day: date) -> dict[str, Any]:
    """የዕለት ተገኝነት መዝገብ — the sheet HR looks at every morning."""
    engine = _engine(conn)
    outages = repo.outage_days(conn, day, day)
    rows = []
    for emp in repo.list_employees(conn):
        punches = repo.load_punches(conn, day, day, emp["id"])
        lm = _leave_map(conn, emp["id"], day, day)
        r = engine.compute_day(
            emp["id"], day, punches,
            on_leave=day in lm, leave_covered=lm.get(day, False),
            outage=day in outages,
        )
        rows.append({
            "ተ.ቁ": len(rows) + 1,
            "የሰራተኛ ስም": emp["name_am"],
            "የስራ መደብ": emp["position_am"],
            "ዘርፍ": emp["department_am"] or "—",
            "የመግቢያ ሰዓት": r.punch_in.strftime("%H:%M") if r.punch_in else "—",
            "የመውጫ ሰዓት": r.punch_out.strftime("%H:%M") if r.punch_out else "—",
            "የሰራው ሰዓት": round(r.worked_hours, 2),
            "ሁኔታ": r.status.label_am,
        })
    eth = EthiopianDate.from_gregorian(day)
    return {
        "title_am": "የዕለት ተገኝነት መዝገብ",
        "period_am": eth.format_am(with_weekday=True),
        "columns": ["ተ.ቁ", "የሰራተኛ ስም", "የስራ መደብ", "ዘርፍ",
                    "የመግቢያ ሰዓት", "የመውጫ ሰዓት", "የሰራው ሰዓት", "ሁኔታ"],
        "rows": rows,
        "summary": _count_statuses(rows),
    }


def _count_statuses(rows: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[r["ሁኔታ"]] = out.get(r["ሁኔታ"], 0) + 1
    return out


# -- 2. monthly summary / payroll ---------------------------------------


def monthly_summary(
    conn: sqlite3.Connection, eth_year: int, eth_month: int
) -> dict[str, Any]:
    """
    ወርሃዊ ማጠቃለያ — the main deliverable, and the payroll input.

    Boundaries follow the ETHIOPIAN month. Cutting on the Gregorian month
    would split every Ethiopian month in the wrong place and make every
    total wrong by a few days.
    """
    start, end = month_range(eth_year, eth_month)
    engine = _engine(conn)
    outages = repo.outage_days(conn, start, end)
    rows = []
    for emp in repo.list_employees(conn):
        punches = repo.load_punches(conn, start, end, emp["id"])
        lm = _leave_map(conn, emp["id"], start, end)
        p = engine.compute_period(
            emp["id"], start, end, punches,
            leave_days=lm, outage_days=outages,
        )
        rows.append({
            "ተ.ቁ": len(rows) + 1,
            "የሰራተኛ ስም": emp["name_am"],
            "የስራ መደብ": emp["position_am"],
            "የሚጠበቅ ሰዓት": p.expected_hours,
            "የሰራው ሰዓት": p.worked_hours,
            "የሚከፈልበት ሰዓት": p.credited_hours,
            "ልዩነት": p.difference,
            "የማሟላት መጠን": f"{p.completion_ratio * 100:.1f}%",
            "የሰራባቸው ቀናት": p.count(DayStatus.WORKED),
            "በፈቃድ ላይ": p.count(DayStatus.ON_LEAVE) + p.count(DayStatus.LEAVE_COVERED),
            "ያልተገኘባቸው": p.count(DayStatus.ABSENT),
        })
    return {
        "title_am": "ወርሃዊ የተገኝነት ማጠቃለያ",
        "period_am": f"{EthiopianDate(eth_year, eth_month, 1).month_name_am} "
                     f"{eth_year} ዓ.ም.",
        "columns": ["ተ.ቁ", "የሰራተኛ ስም", "የስራ መደብ", "የሚጠበቅ ሰዓት",
                    "የሰራው ሰዓት", "የሚከፈልበት ሰዓት", "ልዩነት", "የማሟላት መጠን",
                    "የሰራባቸው ቀናት", "በፈቃድ ላይ", "ያልተገኘባቸው"],
        "rows": rows,
        "totals": {
            "የሚጠበቅ ሰዓት": round(sum(r["የሚጠበቅ ሰዓት"] for r in rows), 2),
            "የሰራው ሰዓት": round(sum(r["የሰራው ሰዓት"] for r in rows), 2),
            "የሚከፈልበት ሰዓት": round(sum(r["የሚከፈልበት ሰዓት"] for r in rows), 2),
        },
        "note_am": "የሚከፈልበት ሰዓት የጸደቀ ፈቃድንና የመርሃ ግብር ቀናትን ያካትታል።",
    }


# -- 3. individual history ----------------------------------------------


def individual_history(
    conn: sqlite3.Connection, employee_id: int, start: date, end: date
) -> dict[str, Any]:
    """የግለሰብ መዝገብ — what a disputed figure expands into."""
    emp = repo.get_employee(conn, employee_id)
    if emp is None:
        raise ValueError(f"no employee {employee_id}")
    engine = _engine(conn)
    punches = repo.load_punches(conn, start, end, employee_id)
    lm = _leave_map(conn, employee_id, start, end)
    outages = repo.outage_days(conn, start, end)
    p = engine.compute_period(
        employee_id, start, end, punches, leave_days=lm, outage_days=outages
    )
    rows = [d.to_row() for d in p.days]
    return {
        "title_am": "የግለሰብ የተገኝነት መዝገብ",
        "employee_am": emp["name_am"],
        "position_am": emp["position_am"],
        "period_am": f"{EthiopianDate.from_gregorian(start).format_am()} — "
                     f"{EthiopianDate.from_gregorian(end).format_am()}",
        "columns": ["ቀን", "ዕለት", "የመግቢያ ሰዓት", "የመውጫ ሰዓት",
                    "የሰራው ሰዓት", "የሚጠበቅ ሰዓት", "ልዩነት", "ሁኔታ"],
        "rows": rows,
        "summary": p.summary_am(),
    }


# -- 4. absence ----------------------------------------------------------


def absence_report(
    conn: sqlite3.Connection, start: date, end: date
) -> dict[str, Any]:
    engine = _engine(conn)
    outages = repo.outage_days(conn, start, end)
    rows = []
    for emp in repo.list_employees(conn):
        punches = repo.load_punches(conn, start, end, emp["id"])
        lm = _leave_map(conn, emp["id"], start, end)
        p = engine.compute_period(
            emp["id"], start, end, punches, leave_days=lm, outage_days=outages
        )
        absent = [d for d in p.days if d.status is DayStatus.ABSENT]
        if not absent:
            continue
        rows.append({
            "ተ.ቁ": len(rows) + 1,
            "የሰራተኛ ስም": emp["name_am"],
            "ያልተገኘባቸው ቀናት": len(absent),
            "የጎደለ ሰዓት": round(sum(d.expected_hours for d in absent), 2),
            "ቀኖቹ": "፣ ".join(
                EthiopianDate.from_gregorian(d.day).format_short_am()
                for d in absent[:10]
            ) + ("..." if len(absent) > 10 else ""),
        })
    return {
        "title_am": "የቀሪዎች ሪፖርት",
        "period_am": f"{EthiopianDate.from_gregorian(start).format_am()} — "
                     f"{EthiopianDate.from_gregorian(end).format_am()}",
        "columns": ["ተ.ቁ", "የሰራተኛ ስም", "ያልተገኘባቸው ቀናት", "የጎደለ ሰዓት", "ቀኖቹ"],
        "rows": rows,
    }


# -- 5. exception log ----------------------------------------------------


def exception_report(
    conn: sqlite3.Connection, start: date, end: date
) -> dict[str, Any]:
    """
    የልዩ ሁኔታዎች መዝገብ — not in the proposal, included anyway.

    PIN use, manual corrections and clock anomalies in one place. This is
    how the system demonstrates it is being used honestly, and it is the
    first thing we will want when someone claims a figure is wrong.
    """
    rows: list[dict[str, Any]] = []

    for r in conn.execute(
        "SELECT e.*, emp.name_am FROM attendance_event e "
        "JOIN employee emp ON emp.id = e.employee_id "
        "WHERE e.work_date BETWEEN ? AND ? AND e.method <> 'face' "
        "ORDER BY e.captured_at",
        (start.isoformat(), end.isoformat()),
    ):
        rows.append({
            "ቀን": EthiopianDate.from_gregorian(
                date.fromisoformat(r["work_date"])).format_short_am(),
            "ዓይነት": "በፒን ኮድ" if r["method"] == "pin" else "በእጅ የገባ",
            "የሰራተኛ ስም": r["name_am"],
            "ዝርዝር": f"{r['event_type']} {r['captured_at'][11:16]}",
            "ምክንያት": r["note"] or r["reason_code"] or "—",
        })

    for r in conn.execute(
        "SELECT * FROM clock_anomaly WHERE detected_at BETWEEN ? AND ? "
        "ORDER BY detected_at",
        (start.isoformat(), (end.isoformat() + "T23:59:59")),
    ):
        rows.append({
            "ቀን": EthiopianDate.from_gregorian(
                date.fromisoformat(r["detected_at"][:10])).format_short_am(),
            "ዓይነት": "የሰዓት ለውጥ",
            "የሰራተኛ ስም": "—",
            "ዝርዝር": f"{r['drift_seconds']:.0f} ሰከንድ ({r['kind']})",
            "ምክንያት": r["note"] or "—",
        })

    return {
        "title_am": "የልዩ ሁኔታዎች መዝገብ",
        "period_am": f"{EthiopianDate.from_gregorian(start).format_am()} — "
                     f"{EthiopianDate.from_gregorian(end).format_am()}",
        "columns": ["ቀን", "ዓይነት", "የሰራተኛ ስም", "ዝርዝር", "ምክንያት"],
        "rows": rows,
        "note_am": "ይህ መዝገብ የስርዓቱን አጠቃቀም ግልጽነት ለማረጋገጥ ነው።",
    }


# -- 6. leave summary ----------------------------------------------------


def leave_summary(conn: sqlite3.Connection, eth_year: int) -> dict[str, Any]:
    rows = []
    for emp in repo.list_employees(conn):
        bal = repo.load_balance(conn, emp["id"], eth_year)
        for line in bal.summary_am():
            rows.append({"የሰራተኛ ስም": emp["name_am"], **line})
    return {
        "title_am": "የፈቃድ ማጠቃለያ",
        "period_am": f"{eth_year} ዓ.ም.",
        "columns": ["የሰራተኛ ስም", "የፈቃድ ዓይነት", "የተፈቀደ", "የተጠቀመ", "ቀሪ"],
        "rows": rows,
    }


# -- 7. task distribution ------------------------------------------------


def task_distribution(
    conn: sqlite3.Connection, start: date, end: date
) -> dict[str, Any]:
    """
    የተግባር ክፍፍል — who is absorbing other people's work.

    Built in from the start rather than added after the first complaint.
    If the same names keep appearing, delegation reads as favouritism or
    as punishment, and this table is what lets a supervisor show otherwise.
    """
    rows = []
    for emp in repo.list_employees(conn):
        received = conn.execute(
            "SELECT COUNT(*) AS n FROM delegated_task "
            "WHERE delegated_to = ? AND covers_from <= ? AND covers_to >= ?",
            (emp["id"], end.isoformat(), start.isoformat()),
        ).fetchone()["n"]
        given = conn.execute(
            "SELECT COUNT(*) AS n FROM delegated_task "
            "WHERE delegated_by = ? AND covers_from <= ? AND covers_to >= ?",
            (emp["id"], end.isoformat(), start.isoformat()),
        ).fetchone()["n"]
        completed = conn.execute(
            "SELECT COUNT(*) AS n FROM delegated_task "
            "WHERE delegated_to = ? AND status = 'completed' "
            "AND covers_from <= ? AND covers_to >= ?",
            (emp["id"], end.isoformat(), start.isoformat()),
        ).fetchone()["n"]
        if received or given:
            rows.append({
                "የሰራተኛ ስም": emp["name_am"],
                "የተቀበላቸው ተግባራት": received,
                "ያጠናቀቃቸው": completed,
                "ያስተላለፋቸው": given,
            })
    return {
        "title_am": "የተግባር ክፍፍል ሪፖርት",
        "period_am": f"{EthiopianDate.from_gregorian(start).format_am()} — "
                     f"{EthiopianDate.from_gregorian(end).format_am()}",
        "columns": ["የሰራተኛ ስም", "የተቀበላቸው ተግባራት", "ያጠናቀቃቸው", "ያስተላለፋቸው"],
        "rows": rows,
    }


REPORTS = {
    "daily": daily_register,
    "monthly": monthly_summary,
    "individual": individual_history,
    "absence": absence_report,
    "exception": exception_report,
    "leave": leave_summary,
    "tasks": task_distribution,
}
