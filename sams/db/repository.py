"""
የመረጃ ቋት ተግባራት — repository layer.

Every write that affects pay goes through here, so the append-only rule,
the hash chain and the audit log are enforced in one place rather than
being remembered at each call site.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Sequence

from ..core.hours import Punch
from ..core.leave import (
    DelegatedTask,
    LeaveBalance,
    LeaveRequest,
    LeaveStatus,
    LeaveType,
    TaskStatus,
)
from ..core.workcalendar import DayType, Holiday, WorkCalendar
from .connection import last_event_hash, row_hash, transaction


# -- audit --------------------------------------------------------------


def audit(
    cur: sqlite3.Cursor,
    admin_user_id: int | None,
    action: str,
    entity: str = "",
    entity_id: Any = "",
    before: Any = None,
    after: Any = None,
    note: str = "",
) -> None:
    cur.execute(
        "INSERT INTO audit_log "
        "(admin_user_id, action, entity, entity_id, before_json, after_json, note) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            admin_user_id,
            action,
            entity,
            str(entity_id),
            json.dumps(before, ensure_ascii=False, default=str) if before else None,
            json.dumps(after, ensure_ascii=False, default=str) if after else None,
            note,
        ),
    )


# -- employees ----------------------------------------------------------


def list_employees(
    conn: sqlite3.Connection, active_only: bool = True
) -> list[sqlite3.Row]:
    sql = (
        "SELECT e.*, d.name_am AS department_am FROM employee e "
        "LEFT JOIN department d ON d.id = e.department_id "
    )
    if active_only:
        sql += "WHERE e.active = 1 "
    sql += "ORDER BY e.name_am"
    return conn.execute(sql).fetchall()


def get_employee(conn: sqlite3.Connection, employee_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT e.*, d.name_am AS department_am FROM employee e "
        "LEFT JOIN department d ON d.id = e.department_id WHERE e.id = ?",
        (employee_id,),
    ).fetchone()


def create_employee(
    conn: sqlite3.Connection, data: dict[str, Any], admin_user_id: int | None = None
) -> int:
    with transaction(conn) as cur:
        cur.execute(
            "INSERT INTO employee "
            "(employee_code, name_am, name_latin, position_am, phone, "
            " department_id, pin_hash, hired_on) "
            "VALUES (:employee_code,:name_am,:name_latin,:position_am,:phone,"
            " :department_id,:pin_hash,:hired_on)",
            {
                "employee_code": data["employee_code"],
                "name_am": data["name_am"],
                "name_latin": data.get("name_latin", ""),
                "position_am": data.get("position_am", ""),
                "phone": data.get("phone", ""),
                "department_id": data.get("department_id"),
                "pin_hash": data.get("pin_hash"),
                "hired_on": data.get("hired_on"),
            },
        )
        new_id = cur.lastrowid
        audit(cur, admin_user_id, "employee.create", "employee", new_id, after=data)
        return new_id


def update_employee(
    conn: sqlite3.Connection,
    employee_id: int,
    changes: dict[str, Any],
    admin_user_id: int | None = None,
) -> None:
    before = get_employee(conn, employee_id)
    if before is None:
        raise ValueError(f"no employee {employee_id}")
    allowed = {
        "name_am", "name_latin", "position_am", "phone",
        "department_id", "pin_hash", "hired_on", "photo_path",
    }
    fields = {k: v for k, v in changes.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    fields["id"] = employee_id
    with transaction(conn) as cur:
        cur.execute(
            f"UPDATE employee SET {sets}, updated_at = datetime('now') WHERE id = :id",
            fields,
        )
        audit(
            cur, admin_user_id, "employee.update", "employee", employee_id,
            before=dict(before), after=changes,
        )


def deactivate_employee(
    conn: sqlite3.Connection, employee_id: int, admin_user_id: int | None = None
) -> None:
    """
    Soft delete only. An employee with attendance history is never removed —
    deleting them would orphan the payroll record that proves what they were
    paid, which is exactly the record a dispute needs.
    """
    with transaction(conn) as cur:
        cur.execute(
            "UPDATE employee SET active = 0, deactivated_on = date('now'), "
            "updated_at = datetime('now') WHERE id = ?",
            (employee_id,),
        )
        audit(cur, admin_user_id, "employee.deactivate", "employee", employee_id)


# -- attendance events --------------------------------------------------


def record_event(
    conn: sqlite3.Connection,
    employee_id: int,
    event_type: str,
    captured_at: datetime,
    monotonic_ns: int,
    *,
    method: str = "face",
    confidence: float | None = None,
    liveness_score: float | None = None,
    image_path: str | None = None,
    work_date: date | None = None,
    created_by: int | None = None,
    reason_code: str = "",
    note: str = "",
) -> int:
    """Append one attendance event, chained to the previous one."""
    wd = (work_date or captured_at.date()).isoformat()
    prev = last_event_hash(conn)
    payload = {
        "employee_id": employee_id,
        "event_type": event_type,
        "captured_at": captured_at.isoformat(),
        "work_date": wd,
        "monotonic_ns": monotonic_ns,
        "method": method,
    }
    h = row_hash(payload, prev)
    with transaction(conn) as cur:
        cur.execute(
            "INSERT INTO attendance_event "
            "(employee_id, event_type, captured_at, work_date, monotonic_ns, "
            " method, confidence, liveness_score, image_path, created_by, "
            " reason_code, note, row_hash, prev_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                employee_id, event_type, captured_at.isoformat(), wd,
                monotonic_ns, method, confidence, liveness_score, image_path,
                created_by, reason_code, note, h, prev,
            ),
        )
        return cur.lastrowid


def recent_event(
    conn: sqlite3.Connection, employee_id: int, within_seconds: int = 60
) -> sqlite3.Row | None:
    """The duplicate-suppression check: did this person just punch?"""
    cutoff = (datetime.now() - timedelta(seconds=within_seconds)).isoformat()
    return conn.execute(
        "SELECT * FROM attendance_event WHERE employee_id = ? "
        "AND superseded_by IS NULL AND captured_at >= ? "
        "ORDER BY captured_at DESC LIMIT 1",
        (employee_id, cutoff),
    ).fetchone()


def next_event_type(conn: sqlite3.Connection, employee_id: int, day: date) -> str:
    """
    Two punches a day: the first is 'in', anything after is 'out'.

    Using last-out rather than refusing a third punch means someone who
    steps out and returns still ends the day with a correct final time,
    which is the reading most favourable to an honest employee.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM attendance_event "
        "WHERE employee_id = ? AND work_date = ? AND event_type = 'in' "
        "AND superseded_by IS NULL",
        (employee_id, day.isoformat()),
    ).fetchone()
    return "out" if row["n"] > 0 else "in"


def supersede_event(
    conn: sqlite3.Connection,
    event_id: int,
    replacement: dict[str, Any],
    admin_user_id: int,
    reason: str,
) -> int:
    """
    Correct a record without destroying it.

    The original row stays exactly as captured and gains a pointer to its
    replacement. Both remain in the hash chain, so the correction is
    visible rather than silent — which is the difference between a
    correction and a falsification.
    """
    before = conn.execute(
        "SELECT * FROM attendance_event WHERE id = ?", (event_id,)
    ).fetchone()
    if before is None:
        raise ValueError(f"no event {event_id}")

    new_id = record_event(
        conn,
        employee_id=replacement.get("employee_id", before["employee_id"]),
        event_type=replacement.get("event_type", before["event_type"]),
        captured_at=replacement["captured_at"],
        monotonic_ns=0,
        method="manual",
        work_date=replacement.get("work_date"),
        created_by=admin_user_id,
        reason_code=replacement.get("reason_code", "correction"),
        note=reason,
    )
    with transaction(conn) as cur:
        cur.execute(
            "UPDATE attendance_event SET superseded_by = ? WHERE id = ?",
            (new_id, event_id),
        )
        audit(
            cur, admin_user_id, "event.supersede", "attendance_event", event_id,
            before=dict(before), after=replacement, note=reason,
        )
    return new_id


def load_punches(
    conn: sqlite3.Connection,
    start: date,
    end: date,
    employee_id: int | None = None,
) -> list[Punch]:
    """Live events only — superseded rows are excluded from every figure."""
    sql = (
        "SELECT id, employee_id, event_type, captured_at, method "
        "FROM attendance_event "
        "WHERE superseded_by IS NULL AND work_date BETWEEN ? AND ? "
    )
    params: list[Any] = [start.isoformat(), end.isoformat()]
    if employee_id is not None:
        sql += "AND employee_id = ? "
        params.append(employee_id)
    sql += "ORDER BY captured_at"
    return [
        Punch(
            employee_id=r["employee_id"],
            at=datetime.fromisoformat(r["captured_at"]),
            kind=r["event_type"],
            method=r["method"],
            event_id=r["id"],
        )
        for r in conn.execute(sql, params)
    ]


def events_for_day(
    conn: sqlite3.Connection, employee_id: int, day: date
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM attendance_event WHERE employee_id = ? AND work_date = ? "
        "ORDER BY captured_at",
        (employee_id, day.isoformat()),
    ).fetchall()


# -- calendar -----------------------------------------------------------


def load_calendar(conn: sqlite3.Connection) -> WorkCalendar:
    """Build the WorkCalendar from stored holidays, overrides and settings."""
    cal = WorkCalendar(holidays=[])

    for r in conn.execute("SELECT * FROM holiday WHERE active = 1"):
        kw: dict[str, Any] = {
            "name_am": r["name_am"],
            "day_type": DayType(r["day_type"]),
            "note_am": r["note_am"],
        }
        if r["eth_month"] is not None:
            kw["ethiopian_month_day"] = (r["eth_month"], r["eth_day"])
        elif r["greg_month"] is not None:
            kw["gregorian_month_day"] = (r["greg_month"], r["greg_day"])
        else:
            kw["explicit_date"] = date.fromisoformat(r["explicit_date"])
        cal.holidays.append(Holiday(**kw))

    for r in conn.execute("SELECT * FROM calendar_override"):
        d = date.fromisoformat(r["work_date"])
        cal.overrides[d] = DayType(r["day_type"])
        if r["note_am"]:
            cal.override_notes[d] = r["note_am"]

    pattern = get_setting(conn, "weekly_pattern")
    if pattern:
        cal.weekly_pattern = {
            int(k): DayType(v) for k, v in json.loads(pattern).items()
        }

    shift = get_setting(conn, "shift")
    if shift:
        from datetime import time as _t

        from ..core.workcalendar import Shift

        s = json.loads(shift)
        cal = WorkCalendar(
            shift=Shift(
                morning_start=_t.fromisoformat(s["morning_start"]),
                morning_end=_t.fromisoformat(s["morning_end"]),
                afternoon_start=_t.fromisoformat(s["afternoon_start"]),
                afternoon_end=_t.fromisoformat(s["afternoon_end"]),
            ),
            weekly_pattern=cal.weekly_pattern,
            holidays=cal.holidays,
            overrides=cal.overrides,
            override_notes=cal.override_notes,
        )
    return cal


def set_day_override(
    conn: sqlite3.Connection,
    day: date,
    day_type: DayType,
    note_am: str,
    admin_user_id: int,
) -> None:
    with transaction(conn) as cur:
        cur.execute(
            "INSERT INTO calendar_override (work_date, day_type, note_am, created_by) "
            "VALUES (?,?,?,?) ON CONFLICT(work_date) DO UPDATE SET "
            "day_type = excluded.day_type, note_am = excluded.note_am",
            (day.isoformat(), day_type.value, note_am, admin_user_id),
        )
        audit(
            cur, admin_user_id, "calendar.override", "calendar_override",
            day.isoformat(), after={"day_type": day_type.value, "note": note_am},
        )


def clear_day_override(
    conn: sqlite3.Connection, day: date, admin_user_id: int
) -> None:
    with transaction(conn) as cur:
        cur.execute(
            "DELETE FROM calendar_override WHERE work_date = ?", (day.isoformat(),)
        )
        audit(
            cur, admin_user_id, "calendar.clear_override",
            "calendar_override", day.isoformat(),
        )


def add_holiday(
    conn: sqlite3.Connection, data: dict[str, Any], admin_user_id: int
) -> int:
    with transaction(conn) as cur:
        cur.execute(
            "INSERT INTO holiday "
            "(name_am, day_type, eth_month, eth_day, greg_month, greg_day, "
            " explicit_date, note_am) VALUES (?,?,?,?,?,?,?,?)",
            (
                data["name_am"], data.get("day_type", "holiday"),
                data.get("eth_month"), data.get("eth_day"),
                data.get("greg_month"), data.get("greg_day"),
                data.get("explicit_date"), data.get("note_am", ""),
            ),
        )
        new_id = cur.lastrowid
        audit(cur, admin_user_id, "holiday.add", "holiday", new_id, after=data)
        return new_id


def outage_days(conn: sqlite3.Connection, start: date, end: date) -> set[date]:
    from ..core.ethiopian import iter_days

    out: set[date] = set()
    for r in conn.execute(
        "SELECT from_date, to_date FROM outage WHERE to_date >= ? AND from_date <= ?",
        (start.isoformat(), end.isoformat()),
    ):
        for d in iter_days(
            date.fromisoformat(r["from_date"]), date.fromisoformat(r["to_date"])
        ):
            if start <= d <= end:
                out.add(d)
    return out


# -- leave --------------------------------------------------------------


def _row_to_leave(
    conn: sqlite3.Connection, r: sqlite3.Row, with_task: bool = True
) -> LeaveRequest:
    task = None
    if with_task:
        t = conn.execute(
            "SELECT * FROM delegated_task WHERE leave_request_id = ?", (r["id"],)
        ).fetchone()
        if t:
            task = DelegatedTask(
                id=t["id"],
                leave_request_id=t["leave_request_id"],
                description=t["description"],
                delegated_to=t["delegated_to"],
                delegated_by=t["delegated_by"],
                covers_from=date.fromisoformat(t["covers_from"]),
                covers_to=date.fromisoformat(t["covers_to"]),
                status=TaskStatus(t["status"]),
                confirmed_by=t["confirmed_by"],
                confirmed_at=(
                    datetime.fromisoformat(t["confirmed_at"])
                    if t["confirmed_at"] else None
                ),
                completion_note=t["completion_note"],
            )
    return LeaveRequest(
        id=r["id"],
        employee_id=r["employee_id"],
        leave_type=LeaveType(r["leave_type"]),
        start_date=date.fromisoformat(r["start_date"]),
        end_date=date.fromisoformat(r["end_date"]),
        reason=r["reason"],
        status=LeaveStatus(r["status"]),
        requested_at=datetime.fromisoformat(r["requested_at"]),
        decided_by=r["decided_by"],
        decided_at=(
            datetime.fromisoformat(r["decided_at"]) if r["decided_at"] else None
        ),
        decision_note=r["decision_note"],
        task=task,
        hours=r["hours"],
    )


def list_leave(
    conn: sqlite3.Connection,
    employee_id: int | None = None,
    status: LeaveStatus | None = None,
    overlapping: tuple[date, date] | None = None,
) -> list[LeaveRequest]:
    sql = "SELECT * FROM leave_request WHERE 1=1 "
    params: list[Any] = []
    if employee_id is not None:
        sql += "AND employee_id = ? "
        params.append(employee_id)
    if status is not None:
        sql += "AND status = ? "
        params.append(status.value)
    if overlapping is not None:
        sql += "AND start_date <= ? AND end_date >= ? "
        params += [overlapping[1].isoformat(), overlapping[0].isoformat()]
    sql += "ORDER BY start_date DESC"
    return [_row_to_leave(conn, r) for r in conn.execute(sql, params)]


def create_leave(
    conn: sqlite3.Connection, req: LeaveRequest, task: DelegatedTask | None = None
) -> int:
    with transaction(conn) as cur:
        cur.execute(
            "INSERT INTO leave_request "
            "(employee_id, leave_type, start_date, end_date, hours, reason, status) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                req.employee_id, req.leave_type.value, req.start_date.isoformat(),
                req.end_date.isoformat(), req.hours, req.reason, req.status.value,
            ),
        )
        leave_id = cur.lastrowid
        if task is not None:
            cur.execute(
                "INSERT INTO delegated_task "
                "(leave_request_id, description, delegated_by, delegated_to, "
                " covers_from, covers_to, status) VALUES (?,?,?,?,?,?,?)",
                (
                    leave_id, task.description, task.delegated_by,
                    task.delegated_to, task.covers_from.isoformat(),
                    task.covers_to.isoformat(), task.status.value,
                ),
            )
        audit(cur, None, "leave.create", "leave_request", leave_id)
        return leave_id


def decide_leave(
    conn: sqlite3.Connection,
    leave_id: int,
    approve: bool,
    admin_user_id: int,
    note: str = "",
) -> None:
    status = LeaveStatus.APPROVED if approve else LeaveStatus.REJECTED
    with transaction(conn) as cur:
        cur.execute(
            "UPDATE leave_request SET status = ?, decided_by = ?, "
            "decided_at = datetime('now'), decision_note = ? WHERE id = ?",
            (status.value, admin_user_id, note, leave_id),
        )
        audit(
            cur, admin_user_id, f"leave.{status.value}", "leave_request",
            leave_id, note=note,
        )


def confirm_task(
    conn: sqlite3.Connection,
    task_id: int,
    completed: bool,
    admin_user_id: int,
    note: str = "",
) -> None:
    """
    Confirming a delegated task is what turns a leave day into a paid one,
    so it is an administrative action with a named actor and an audit row,
    never something the employee on leave can do for themselves.
    """
    status = TaskStatus.COMPLETED if completed else TaskStatus.NOT_DONE
    with transaction(conn) as cur:
        cur.execute(
            "UPDATE delegated_task SET status = ?, confirmed_by = ?, "
            "confirmed_at = datetime('now'), completion_note = ? WHERE id = ?",
            (status.value, admin_user_id, note, task_id),
        )
        audit(
            cur, admin_user_id, f"task.{status.value}", "delegated_task",
            task_id, note=note,
        )


def open_task_count(conn: sqlite3.Connection, employee_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM delegated_task WHERE delegated_to = ? "
        "AND status IN ('assigned','progress')",
        (employee_id,),
    ).fetchone()["n"]


def load_balance(
    conn: sqlite3.Connection, employee_id: int, eth_year: int
) -> LeaveBalance:
    from ..core.leave import DEFAULT_ENTITLEMENTS, LeaveService

    ents: dict[LeaveType, float] = {}
    for r in conn.execute(
        "SELECT leave_type, days FROM leave_entitlement "
        "WHERE employee_id = ? AND eth_year = ?",
        (employee_id, eth_year),
    ):
        ents[LeaveType(r["leave_type"])] = r["days"]
    if not ents:
        ents = dict(DEFAULT_ENTITLEMENTS)

    cal = load_calendar(conn)
    svc = LeaveService(cal)
    used: dict[LeaveType, float] = {}
    for req in list_leave(conn, employee_id, LeaveStatus.APPROVED):
        if LeaveService.balance_year_for(req.start_date) != eth_year:
            continue
        cost = req.balance_cost(cal)
        if cost:
            used[req.leave_type] = used.get(req.leave_type, 0.0) + cost

    return LeaveBalance(
        employee_id=employee_id, eth_year=eth_year, entitlements=ents, used=used
    )


def set_entitlement(
    conn: sqlite3.Connection,
    employee_id: int,
    eth_year: int,
    leave_type: LeaveType,
    days: float,
    admin_user_id: int,
) -> None:
    with transaction(conn) as cur:
        cur.execute(
            "INSERT INTO leave_entitlement (employee_id, eth_year, leave_type, days) "
            "VALUES (?,?,?,?) ON CONFLICT(employee_id, eth_year, leave_type) "
            "DO UPDATE SET days = excluded.days",
            (employee_id, eth_year, leave_type.value, days),
        )
        audit(
            cur, admin_user_id, "entitlement.set", "leave_entitlement",
            f"{employee_id}/{eth_year}/{leave_type.value}", after={"days": days},
        )


# -- settings -----------------------------------------------------------


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    r = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(
    conn: sqlite3.Connection, key: str, value: str, admin_user_id: int | None = None
) -> None:
    with transaction(conn) as cur:
        cur.execute(
            "INSERT INTO setting (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = datetime('now')",
            (key, value),
        )
        audit(cur, admin_user_id, "setting.set", "setting", key, after={"value": value})
