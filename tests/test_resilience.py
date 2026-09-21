"""
Destructive tests for crash safety, clock tampering and record integrity.

The office loses power one to two hours most days. These tests kill the
process mid-write, repeatedly, and require the database to open clean every
single time. If this suite passes, the deployment survives the year.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from sams.db.connection import (
    MonotonicClock,
    backup_to,
    connect,
    init_schema,
    integrity_check,
    last_event_hash,
    log_backup,
    row_hash,
    transaction,
    verify_hash_chain,
)
from sams.db.repository import (
    create_employee,
    load_punches,
    next_event_type,
    record_event,
    recent_event,
    supersede_event,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "sams.db")
    init_schema(conn)
    conn.execute(
        "INSERT INTO admin_user (id, username, password_hash, role) "
        "VALUES (1, 'admin', 'x', 'admin')"
    )
    conn.commit()
    create_employee(conn, {"employee_code": "W-001", "name_am": "አበበ ከበደ"})
    yield conn
    conn.close()


# -- pragmas actually applied -------------------------------------------


def test_wal_and_full_sync_are_on(db):
    assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert db.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_foreign_keys_are_enforced(db):
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(db) as cur:
            cur.execute(
                "INSERT INTO attendance_event "
                "(employee_id, event_type, captured_at, work_date, monotonic_ns) "
                "VALUES (9999, 'in', ?, ?, 0)",
                (datetime.now().isoformat(), date.today().isoformat()),
            )


# -- power loss ----------------------------------------------------------

_KILL_SCRIPT = textwrap.dedent(
    """
    import os, sys
    from datetime import datetime, date
    sys.path.insert(0, {repo!r})
    from sams.db.connection import connect
    from sams.db.repository import record_event

    conn = connect({db!r})
    n = 0
    while True:
        record_event(conn, 1, 'in', datetime.now(), n)
        n += 1
        if n == {kill_after}:
            # Simulate the power going out: no flush, no close, no cleanup.
            os._exit(9)
    """
)


@pytest.mark.parametrize("kill_after", [1, 3, 7, 11, 17])
def test_database_survives_being_killed_mid_write(tmp_path, kill_after):
    """
    Hard-kill the writer with os._exit (no cleanup, no flush — the closest
    thing to pulling the plug) and require the database to reopen clean
    with every acknowledged write still present.
    """
    db_path = tmp_path / f"kill{kill_after}.db"
    conn = connect(db_path)
    init_schema(conn)
    create_employee(conn, {"employee_code": "W-001", "name_am": "አበበ"})
    conn.close()

    script = _KILL_SCRIPT.format(
        repo=str(REPO_ROOT), db=str(db_path), kill_after=kill_after
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, timeout=60
    )
    assert proc.returncode == 9, proc.stderr.decode()[-500:]

    reopened = connect(db_path)
    try:
        assert integrity_check(reopened), "database corrupted by power loss"
        n = reopened.execute(
            "SELECT COUNT(*) AS n FROM attendance_event"
        ).fetchone()["n"]
        # Every committed write must have survived the kill.
        assert n == kill_after
        assert verify_hash_chain(reopened) == []
    finally:
        reopened.close()


def test_twenty_consecutive_kills_leave_a_clean_database(tmp_path):
    """
    The test that matters most. If twenty deliberate kills cannot corrupt
    it, a year of daily outages will not either.
    """
    db_path = tmp_path / "repeat.db"
    conn = connect(db_path)
    init_schema(conn)
    create_employee(conn, {"employee_code": "W-001", "name_am": "አበበ"})
    conn.close()

    total = 0
    for i in range(20):
        script = _KILL_SCRIPT.format(
            repo=str(REPO_ROOT), db=str(db_path), kill_after=2
        )
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, timeout=60
        )
        assert proc.returncode == 9
        total += 2
        c = connect(db_path)
        try:
            assert integrity_check(c), f"corrupted after kill {i + 1}"
            n = c.execute("SELECT COUNT(*) AS n FROM attendance_event").fetchone()["n"]
            assert n == total, f"lost a committed write at kill {i + 1}"
        finally:
            c.close()


def test_rollback_leaves_no_partial_row(db):
    before = db.execute("SELECT COUNT(*) AS n FROM attendance_event").fetchone()["n"]
    with pytest.raises(RuntimeError):
        with transaction(db) as cur:
            cur.execute(
                "INSERT INTO attendance_event "
                "(employee_id, event_type, captured_at, work_date, monotonic_ns) "
                "VALUES (1,'in',?,?,0)",
                (datetime.now().isoformat(), date.today().isoformat()),
            )
            raise RuntimeError("power cut mid-transaction")
    after = db.execute("SELECT COUNT(*) AS n FROM attendance_event").fetchone()["n"]
    assert after == before


# -- append-only and the hash chain --------------------------------------


def test_hash_chain_is_valid_for_a_normal_day(db):
    now = datetime(2026, 9, 21, 8, 30)
    record_event(db, 1, "in", now, 1)
    record_event(db, 1, "out", now + timedelta(hours=9), 2)
    assert verify_hash_chain(db) == []


def test_editing_an_event_behind_the_systems_back_is_detected(db):
    """
    Someone with file access changes a stored time directly in SQLite.
    The chain must expose it — the row's own hash no longer matches.
    """
    record_event(db, 1, "in", datetime(2026, 9, 21, 9, 30), 1)
    record_event(db, 1, "out", datetime(2026, 9, 21, 17, 30), 2)
    assert verify_hash_chain(db) == []

    db.execute(
        "UPDATE attendance_event SET captured_at = ? WHERE id = 1",
        (datetime(2026, 9, 21, 8, 30).isoformat(),),
    )
    db.commit()

    broken = verify_hash_chain(db)
    assert 1 in broken, "a silent edit went undetected"


def test_deleting_an_event_breaks_the_chain(db):
    for i in range(3):
        record_event(db, 1, "in", datetime(2026, 9, 21, 8, i), i)
    db.execute("DELETE FROM attendance_event WHERE id = 2")
    db.commit()
    assert verify_hash_chain(db) != []


def test_correction_supersedes_rather_than_overwrites(db):
    original = record_event(db, 1, "in", datetime(2026, 9, 21, 9, 30), 1)
    new_id = supersede_event(
        db, original,
        {"captured_at": datetime(2026, 9, 21, 8, 30)},
        admin_user_id=1, reason="የካሜራ ብልሽት",
    )
    row = db.execute(
        "SELECT * FROM attendance_event WHERE id = ?", (original,)
    ).fetchone()
    assert row is not None, "the original was destroyed"
    assert row["superseded_by"] == new_id
    assert row["captured_at"].endswith("09:30:00")  # untouched

    audit_rows = db.execute(
        "SELECT * FROM audit_log WHERE action = 'event.supersede'"
    ).fetchall()
    assert len(audit_rows) == 1
    assert "የካሜራ ብልሽት" in audit_rows[0]["note"]


def test_superseded_events_are_excluded_from_figures(db):
    original = record_event(db, 1, "in", datetime(2026, 9, 21, 9, 30), 1)
    supersede_event(
        db, original, {"captured_at": datetime(2026, 9, 21, 8, 30)},
        admin_user_id=1, reason="correction",
    )
    punches = load_punches(db, date(2026, 9, 21), date(2026, 9, 21))
    assert len(punches) == 1
    assert punches[0].at.hour == 8


# -- duplicate suppression -----------------------------------------------


def test_rapid_repeat_punch_is_detectable(db):
    record_event(db, 1, "in", datetime.now(), 1)
    assert recent_event(db, 1, within_seconds=60) is not None


def test_first_punch_is_in_and_second_is_out(db):
    day = date(2026, 9, 21)
    assert next_event_type(db, 1, day) == "in"
    record_event(db, 1, "in", datetime(2026, 9, 21, 8, 30), 1, work_date=day)
    assert next_event_type(db, 1, day) == "out"
    record_event(db, 1, "out", datetime(2026, 9, 21, 17, 30), 2, work_date=day)
    assert next_event_type(db, 1, day) == "out"   # later taps update the exit


def test_a_new_day_starts_with_in_again(db):
    record_event(
        db, 1, "in", datetime(2026, 9, 21, 8, 30), 1, work_date=date(2026, 9, 21)
    )
    assert next_event_type(db, 1, date(2026, 9, 22)) == "in"


# -- clock tampering ------------------------------------------------------


def test_clock_reports_no_drift_when_untouched():
    clock = MonotonicClock()
    ok, drift, kind = clock.check()
    assert ok and kind == "ok" and abs(drift) < 5


def test_backward_clock_change_is_detected():
    """
    The attack: set the clock back an hour, punch in, set it forward.
    The monotonic counter cannot be moved, so the discrepancy shows.
    """
    clock = MonotonicClock()
    clock._t0_wall = datetime.now() + timedelta(hours=1)  # as if the clock moved back
    ok, drift, kind = clock.check()
    assert not ok
    assert kind == "backward"
    assert drift < -3000


def test_forward_clock_change_is_detected():
    clock = MonotonicClock()
    clock._t0_wall = datetime.now() - timedelta(hours=2)
    ok, drift, kind = clock.check()
    assert not ok and kind == "forward"


def test_resync_clears_the_anomaly():
    clock = MonotonicClock()
    clock._t0_wall = datetime.now() + timedelta(hours=1)
    assert not clock.check()[0]
    clock.resync()
    assert clock.check()[0]


def test_monotonic_counter_never_decreases():
    clock = MonotonicClock()
    values = [clock.now()[1] for _ in range(200)]
    assert values == sorted(values)


# -- backup ---------------------------------------------------------------


def test_backup_is_verified_and_restorable(db, tmp_path):
    for i in range(5):
        record_event(db, 1, "in", datetime(2026, 9, 21, 8, i), i)

    result = backup_to(db, tmp_path / "backup" / "sams-backup.db")
    assert result["verified"] == 1
    assert result["bytes"] > 0

    restored = connect(tmp_path / "backup" / "sams-backup.db")
    try:
        assert integrity_check(restored)
        assert restored.execute(
            "SELECT COUNT(*) AS n FROM attendance_event"
        ).fetchone()["n"] == 5
        assert verify_hash_chain(restored) == []
    finally:
        restored.close()


def test_backup_log_records_the_verification(db, tmp_path):
    result = backup_to(db, tmp_path / "b.db")
    log_backup(db, result)
    row = db.execute("SELECT * FROM backup_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["verified"] == 1
    assert "employee" in row["row_counts"]


def test_backup_to_a_missing_directory_creates_it(db, tmp_path):
    result = backup_to(db, tmp_path / "deep" / "nested" / "b.db")
    assert Path(result["destination"]).exists()


def test_backup_overwrites_an_existing_file(db, tmp_path):
    dest = tmp_path / "b.db"
    dest.write_bytes(b"not a database at all")
    result = backup_to(db, dest)
    assert result["verified"] == 1


# -- injection and hostile input -----------------------------------------


def test_sql_metacharacters_in_a_name_are_stored_literally(db):
    nasty = "'; DROP TABLE employee; --"
    emp_id = create_employee(db, {"employee_code": "W-999", "name_am": nasty})
    row = db.execute("SELECT name_am FROM employee WHERE id = ?", (emp_id,)).fetchone()
    assert row["name_am"] == nasty
    assert db.execute(
        "SELECT COUNT(*) AS n FROM employee"
    ).fetchone()["n"] >= 1  # table still exists


def test_script_tag_in_an_amharic_name_is_stored_not_executed(db):
    payload = "አበበ<script>alert(1)</script>"
    emp_id = create_employee(db, {"employee_code": "W-998", "name_am": payload})
    row = db.execute("SELECT name_am FROM employee WHERE id = ?", (emp_id,)).fetchone()
    assert row["name_am"] == payload  # escaping is the template's job, not the DB's


def test_duplicate_employee_code_is_refused(db):
    create_employee(db, {"employee_code": "W-777", "name_am": "አለም"})
    with pytest.raises(sqlite3.IntegrityError):
        create_employee(db, {"employee_code": "W-777", "name_am": "ሌላ"})


def test_event_type_is_constrained(db):
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(db) as cur:
            cur.execute(
                "INSERT INTO attendance_event "
                "(employee_id, event_type, captured_at, work_date, monotonic_ns) "
                "VALUES (1,'sideways',?,?,0)",
                (datetime.now().isoformat(), date.today().isoformat()),
            )


def test_task_cannot_be_delegated_to_self_at_the_database_level(db):
    create_employee(db, {"employee_code": "W-002", "name_am": "ሰላም"})
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(db) as cur:
            cur.execute(
                "INSERT INTO delegated_task "
                "(description, delegated_by, delegated_to, covers_from, covers_to) "
                "VALUES ('x', 1, 1, '2026-09-21', '2026-09-21')"
            )


def test_leave_end_before_start_is_refused_at_the_database_level(db):
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(db) as cur:
            cur.execute(
                "INSERT INTO leave_request "
                "(employee_id, leave_type, start_date, end_date) "
                "VALUES (1,'annual','2026-09-25','2026-09-21')"
            )
