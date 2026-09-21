-- SAMS — የመረጃ ቋት መዋቅር (database schema)
--
-- Design rules enforced here:
--   * attendance_event is APPEND-ONLY. Corrections insert a new row and set
--     superseded_by on the old one. Nothing is ever updated in place, so a
--     payroll figure can always be traced back to the scan that produced it.
--   * Every event stores BOTH the wall clock and a monotonic counter, so a
--     tampered system clock is detectable after the fact.
--   * Every row that affects pay carries a hash chained to the previous row,
--     making silent edits to the database file detectable.
--   * Employees are never hard-deleted once they have attendance history.

PRAGMA foreign_keys = ON;

-- ጾታ / department ------------------------------------------------------
CREATE TABLE IF NOT EXISTS department (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name_am      TEXT    NOT NULL UNIQUE,
    name_latin   TEXT    NOT NULL DEFAULT '',
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ሰራተኛ / employee ------------------------------------------------------
CREATE TABLE IF NOT EXISTS employee (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_code  TEXT    NOT NULL UNIQUE,
    name_am        TEXT    NOT NULL,
    name_latin     TEXT    NOT NULL DEFAULT '',
    position_am    TEXT    NOT NULL DEFAULT '',
    phone          TEXT    NOT NULL DEFAULT '',
    department_id  INTEGER REFERENCES department(id),
    pin_hash       TEXT,
    photo_path     TEXT,
    hired_on       TEXT,
    active         INTEGER NOT NULL DEFAULT 1,
    deactivated_on TEXT,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_employee_active ON employee(active);
CREATE INDEX IF NOT EXISTS ix_employee_dept   ON employee(department_id);

-- የፊት መለያ / face embeddings -------------------------------------------
-- Several rows per employee. Loaded into one NumPy matrix at startup;
-- at 30 employees the whole matrix is a few hundred kilobytes.
CREATE TABLE IF NOT EXISTS face_embedding (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id   INTEGER NOT NULL REFERENCES employee(id) ON DELETE CASCADE,
    vector        BLOB    NOT NULL,          -- float32 little-endian, 512-d
    dim           INTEGER NOT NULL DEFAULT 512,
    model         TEXT    NOT NULL DEFAULT 'buffalo_l',
    quality       REAL    NOT NULL DEFAULT 0,
    image_path    TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_embedding_employee ON face_embedding(employee_id);

-- የተገኝነት መዝገብ / attendance events — APPEND ONLY ------------------------
CREATE TABLE IF NOT EXISTS attendance_event (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id    INTEGER NOT NULL REFERENCES employee(id),
    event_type     TEXT    NOT NULL CHECK (event_type IN ('in','out')),
    captured_at    TEXT    NOT NULL,         -- ISO local wall clock
    work_date      TEXT    NOT NULL,         -- ISO date the event belongs to
    monotonic_ns   INTEGER NOT NULL,         -- clock-tamper detection
    method         TEXT    NOT NULL DEFAULT 'face'
                   CHECK (method IN ('face','pin','manual')),
    confidence     REAL,
    liveness_score REAL,
    image_path     TEXT,                     -- the evidence for disputes
    created_by     INTEGER REFERENCES admin_user(id),  -- manual entries only
    reason_code    TEXT    NOT NULL DEFAULT '',
    note           TEXT    NOT NULL DEFAULT '',
    superseded_by  INTEGER REFERENCES attendance_event(id),
    row_hash       TEXT    NOT NULL DEFAULT '',
    prev_hash      TEXT    NOT NULL DEFAULT '',
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_event_emp_date ON attendance_event(employee_id, work_date);
CREATE INDEX IF NOT EXISTS ix_event_date     ON attendance_event(work_date);
CREATE INDEX IF NOT EXISTS ix_event_live     ON attendance_event(work_date)
    WHERE superseded_by IS NULL;

-- የስራ ቀን አይነት / work calendar overrides ------------------------------
CREATE TABLE IF NOT EXISTS calendar_override (
    work_date   TEXT PRIMARY KEY,
    day_type    TEXT NOT NULL CHECK (day_type IN
                ('full','half_am','half_pm','off','holiday','event')),
    note_am     TEXT NOT NULL DEFAULT '',
    created_by  INTEGER REFERENCES admin_user(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- የህዝብ በዓላት / holidays -------------------------------------------------
-- Addressed in Ethiopian terms, Gregorian terms, or as an explicit date
-- for movable feasts (ፋሲካ, ዒድ) that HR enters each year.
CREATE TABLE IF NOT EXISTS holiday (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name_am        TEXT    NOT NULL,
    day_type       TEXT    NOT NULL DEFAULT 'holiday',
    eth_month      INTEGER,
    eth_day        INTEGER,
    greg_month     INTEGER,
    greg_day       INTEGER,
    explicit_date  TEXT,
    note_am        TEXT    NOT NULL DEFAULT '',
    active         INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK (
        (eth_month IS NOT NULL AND eth_day IS NOT NULL
         AND greg_month IS NULL AND explicit_date IS NULL)
     OR (greg_month IS NOT NULL AND greg_day IS NOT NULL
         AND eth_month IS NULL AND explicit_date IS NULL)
     OR (explicit_date IS NOT NULL
         AND eth_month IS NULL AND greg_month IS NULL)
    )
);

-- ፈቃድ / leave ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS leave_entitlement (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id  INTEGER NOT NULL REFERENCES employee(id) ON DELETE CASCADE,
    eth_year     INTEGER NOT NULL,
    leave_type   TEXT    NOT NULL,
    days         REAL    NOT NULL DEFAULT 0,
    UNIQUE (employee_id, eth_year, leave_type)
);

CREATE TABLE IF NOT EXISTS leave_request (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id   INTEGER NOT NULL REFERENCES employee(id),
    leave_type    TEXT    NOT NULL,
    start_date    TEXT    NOT NULL,
    end_date      TEXT    NOT NULL,
    hours         REAL,
    reason        TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','approved','rejected','cancelled')),
    requested_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    decided_by    INTEGER REFERENCES admin_user(id),
    decided_at    TEXT,
    decision_note TEXT    NOT NULL DEFAULT '',
    CHECK (end_date >= start_date)
);
CREATE INDEX IF NOT EXISTS ix_leave_emp    ON leave_request(employee_id, status);
CREATE INDEX IF NOT EXISTS ix_leave_period ON leave_request(start_date, end_date);

-- የተግባር ማስተላለፍ / delegated tasks -------------------------------------
CREATE TABLE IF NOT EXISTS delegated_task (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    leave_request_id  INTEGER REFERENCES leave_request(id) ON DELETE CASCADE,
    description       TEXT    NOT NULL,
    delegated_by      INTEGER NOT NULL REFERENCES employee(id),
    delegated_to      INTEGER NOT NULL REFERENCES employee(id),
    covers_from       TEXT    NOT NULL,
    covers_to         TEXT    NOT NULL,
    status            TEXT    NOT NULL DEFAULT 'assigned'
                      CHECK (status IN ('assigned','progress','completed','not_done')),
    confirmed_by      INTEGER REFERENCES admin_user(id),
    confirmed_at      TEXT,
    completion_note   TEXT    NOT NULL DEFAULT '',
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK (delegated_to <> delegated_by),
    CHECK (covers_to >= covers_from)
);
CREATE INDEX IF NOT EXISTS ix_task_to ON delegated_task(delegated_to, status);

-- ተጠቃሚዎች / admin users ------------------------------------------------
CREATE TABLE IF NOT EXISTS admin_user (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT    NOT NULL UNIQUE,
    display_name_am TEXT    NOT NULL DEFAULT '',
    password_hash   TEXT    NOT NULL,
    role            TEXT    NOT NULL CHECK (role IN ('viewer','hr','admin')),
    employee_id     INTEGER REFERENCES employee(id),
    must_change_pw  INTEGER NOT NULL DEFAULT 1,
    active          INTEGER NOT NULL DEFAULT 1,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT,
    last_login_at   TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- የክትትል መዝገብ / audit log ---------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_user_id INTEGER REFERENCES admin_user(id),
    action        TEXT    NOT NULL,
    entity        TEXT    NOT NULL DEFAULT '',
    entity_id     TEXT    NOT NULL DEFAULT '',
    before_json   TEXT,
    after_json    TEXT,
    note          TEXT    NOT NULL DEFAULT '',
    at            TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_audit_at ON audit_log(at);

-- የሰዓት ችግር / clock anomalies ------------------------------------------
CREATE TABLE IF NOT EXISTS clock_anomaly (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    expected_at   TEXT    NOT NULL,
    observed_at   TEXT    NOT NULL,
    drift_seconds REAL    NOT NULL,
    kind          TEXT    NOT NULL,
    note          TEXT    NOT NULL DEFAULT '',
    acknowledged  INTEGER NOT NULL DEFAULT 0
);

-- የስርዓት ቅንብሮች / settings ----------------------------------------------
CREATE TABLE IF NOT EXISTS setting (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ምትኬ / backup log -----------------------------------------------------
CREATE TABLE IF NOT EXISTS backup_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT    NOT NULL,
    finished_at  TEXT,
    destination  TEXT    NOT NULL,
    bytes        INTEGER,
    row_counts   TEXT,
    verified     INTEGER NOT NULL DEFAULT 0,
    error        TEXT    NOT NULL DEFAULT ''
);

-- የኤሌክትሪክ መቋረጥ / recorded outages ------------------------------------
CREATE TABLE IF NOT EXISTS outage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    from_date   TEXT NOT NULL,
    to_date     TEXT NOT NULL,
    note_am     TEXT NOT NULL DEFAULT '',
    created_by  INTEGER REFERENCES admin_user(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (to_date >= from_date)
);

CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
