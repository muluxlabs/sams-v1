"""
የመረጃ ቋት ግንኙነት — database connection and crash safety.

The office loses power for one to two hours most days. The database must
survive being killed mid-write, every time, without exception. That is what
the pragmas here are for:

  journal_mode = WAL      writers do not block readers; the kiosk keeps
                          recording while a report is being generated
  synchronous = FULL      every commit reaches the disk platter before it is
                          acknowledged. Slower, and non-negotiable when the
                          machine can lose power at any instant
  foreign_keys = ON       SQLite disables these by default, per connection

Do not relax `synchronous` to NORMAL for speed. At 120 events a day the
write cost is irrelevant, and NORMAL can lose the last transactions on
power loss — which here means someone's check-in.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time as _time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode = WAL")
    cur.execute("PRAGMA synchronous = FULL")
    cur.execute("PRAGMA foreign_keys = ON")
    cur.execute("PRAGMA busy_timeout = 5000")
    # Keep temp tables in memory; the disk may be slow or nearly full.
    cur.execute("PRAGMA temp_store = MEMORY")
    cur.close()


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        isolation_level=None,      # explicit transactions only
        check_same_thread=False,
    )
    _configure(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute(
        "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
        (SCHEMA_VERSION,),
    )


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Cursor]:
    """
    An explicit IMMEDIATE transaction.

    IMMEDIATE rather than DEFERRED so a write lock is taken up front: two
    processes (the kiosk and a report job) cannot then deadlock halfway
    through. On any exception the whole transaction rolls back, which is
    what keeps a half-written attendance event from ever existing.
    """
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    try:
        yield cur
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        cur.close()


# -- integrity ---------------------------------------------------------


def row_hash(payload: dict[str, Any], prev_hash: str) -> str:
    """
    Chain hash over an attendance event.

    Cheap to compute and it makes a silent edit to the database file
    detectable: changing any event breaks every hash after it. This does
    not prevent tampering — nothing on an offline machine can — but it
    means tampering cannot go unnoticed, which is what matters when these
    rows determine someone's pay.
    """
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256((prev_hash + blob).encode("utf-8")).hexdigest()


def last_event_hash(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT row_hash FROM attendance_event ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["row_hash"] if row else ""


def verify_hash_chain(conn: sqlite3.Connection) -> list[int]:
    """Return the ids of events whose hash does not match the chain."""
    broken: list[int] = []
    prev = ""
    for row in conn.execute(
        "SELECT id, employee_id, event_type, captured_at, work_date, "
        "monotonic_ns, method, row_hash, prev_hash "
        "FROM attendance_event ORDER BY id"
    ):
        payload = {
            "employee_id": row["employee_id"],
            "event_type": row["event_type"],
            "captured_at": row["captured_at"],
            "work_date": row["work_date"],
            "monotonic_ns": row["monotonic_ns"],
            "method": row["method"],
        }
        expected = row_hash(payload, prev)
        if expected != row["row_hash"] or row["prev_hash"] != prev:
            broken.append(row["id"])
        prev = row["row_hash"]
    return broken


def integrity_check(conn: sqlite3.Connection) -> bool:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return row[0] == "ok"


# -- clock -------------------------------------------------------------


class MonotonicClock:
    """
    Pairs the wall clock with a counter that cannot be moved backwards.

    An offline machine has no NTP and its clock is settable by anyone with
    local access. Set it back an hour, punch in, set it forward again, and
    a short day becomes a full one. The monotonic counter cannot be reset
    by changing the system clock, so comparing the two exposes the change.
    """

    def __init__(self) -> None:
        self._t0_wall = datetime.now()
        self._t0_mono = _time.monotonic_ns()

    def now(self) -> tuple[datetime, int]:
        return datetime.now(), _time.monotonic_ns()

    def expected_wall(self) -> datetime:
        from datetime import timedelta

        elapsed_ns = _time.monotonic_ns() - self._t0_mono
        return self._t0_wall + timedelta(microseconds=elapsed_ns / 1000)

    def drift_seconds(self) -> float:
        return (datetime.now() - self.expected_wall()).total_seconds()

    def check(self, tolerance_seconds: float = 120.0) -> tuple[bool, float, str]:
        """(ok, drift, kind). Negative drift means the clock moved backwards."""
        drift = self.drift_seconds()
        if abs(drift) <= tolerance_seconds:
            return True, drift, "ok"
        return False, drift, "backward" if drift < 0 else "forward"

    def resync(self) -> None:
        """Accept the current wall clock as the new baseline, after an
        anomaly has been recorded and acknowledged."""
        self._t0_wall = datetime.now()
        self._t0_mono = _time.monotonic_ns()


def record_clock_anomaly(
    conn: sqlite3.Connection,
    expected: datetime,
    observed: datetime,
    drift: float,
    kind: str,
    note: str = "",
) -> None:
    with transaction(conn) as cur:
        cur.execute(
            "INSERT INTO clock_anomaly "
            "(expected_at, observed_at, drift_seconds, kind, note) "
            "VALUES (?,?,?,?,?)",
            (expected.isoformat(), observed.isoformat(), drift, kind, note),
        )


# -- backup ------------------------------------------------------------


def backup_to(conn: sqlite3.Connection, destination: str | Path) -> dict[str, Any]:
    """
    A consistent snapshot without stopping the service, then verified by
    opening the copy and counting rows.

    An unverified backup is a guess. This returns the counts so the caller
    can write them to backup_log and show them on the dashboard, because
    HR staff will not read a log file but they will notice a red banner.
    """
    dest = Path(destination)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()

    started = datetime.now()
    target = sqlite3.connect(str(dest))
    try:
        conn.backup(target)
    finally:
        target.close()

    # Verify by reopening the copy and counting what matters.
    counts: dict[str, int] = {}
    check = sqlite3.connect(str(dest))
    check.row_factory = sqlite3.Row
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("backup failed integrity_check")
        for table in (
            "employee", "attendance_event", "face_embedding",
            "leave_request", "delegated_task", "audit_log",
        ):
            counts[table] = check.execute(
                f"SELECT COUNT(*) AS n FROM {table}"
            ).fetchone()["n"]
    finally:
        check.close()

    # The copy must match the live database, or the backup is not usable.
    for table, n in counts.items():
        live = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        if live != n:
            raise RuntimeError(
                f"backup verification failed: {table} live={live} copy={n}"
            )

    return {
        "started_at": started.isoformat(),
        "finished_at": datetime.now().isoformat(),
        "destination": str(dest),
        "bytes": dest.stat().st_size,
        "row_counts": json.dumps(counts, ensure_ascii=False),
        "verified": 1,
    }


def log_backup(conn: sqlite3.Connection, result: dict[str, Any]) -> None:
    with transaction(conn) as cur:
        cur.execute(
            "INSERT INTO backup_log "
            "(started_at, finished_at, destination, bytes, row_counts, verified, error) "
            "VALUES (:started_at,:finished_at,:destination,:bytes,:row_counts,"
            ":verified, '')",
            result,
        )


def last_verified_backup(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM backup_log WHERE verified = 1 "
        "ORDER BY finished_at DESC LIMIT 1"
    ).fetchone()
