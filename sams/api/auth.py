"""
የመግቢያ ቁጥጥር — authentication and roles.

Three roles, because "controlled administrator access" in practice means
several people with different reasons to be in the system:

  ተመልካች (viewer)  dashboard and reports, changes nothing
  የሰው ሃይል (hr)     the above, plus corrections, employees, leave decisions
  አስተዳዳሪ (admin)   everything, including users, settings and the calendar

Passwords use PBKDF2-HMAC-SHA256 from the standard library rather than
Argon2, so the installer has no compiled dependency to fail on a Woreda
machine. 600,000 iterations is deliberate: logins happen a few times a day,
so the cost is invisible to users and expensive to an attacker with the
database file.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Literal

Role = Literal["viewer", "hr", "admin"]

_ITERATIONS = 600_000
_LOCK_THRESHOLD = 5
_LOCK_MINUTES = 15
SESSION_HOURS = 8

# What each role may do. Checked centrally so a new endpoint cannot
# accidentally be left unguarded.
PERMISSIONS: dict[str, set[Role]] = {
    "view_dashboard":   {"viewer", "hr", "admin"},
    "view_reports":     {"viewer", "hr", "admin"},
    "view_employees":   {"viewer", "hr", "admin"},
    "view_own_leave":   {"viewer", "hr", "admin"},
    "manage_employees": {"hr", "admin"},
    "enroll_faces":     {"hr", "admin"},
    "correct_records":  {"hr", "admin"},
    "decide_leave":     {"hr", "admin"},
    "confirm_tasks":    {"hr", "admin"},
    "manage_calendar":  {"hr", "admin"},
    "bulk_entry":       {"hr", "admin"},
    "manage_users":     {"admin"},
    "manage_settings":  {"admin"},
    "run_backup":       {"admin"},
    "calibrate":        {"admin"},
}


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iters)
        )
        # Constant-time: a timing difference would leak the hash prefix.
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


def can(role: str, permission: str) -> bool:
    return role in PERMISSIONS.get(permission, set())


class AuthError(Exception):
    def __init__(self, message_am: str) -> None:
        self.message_am = message_am
        super().__init__(message_am)


class SessionStore:
    """
    In-memory sessions.

    Deliberately not persisted: a restart logs everyone out, which on a
    shared office machine is the safe default rather than an inconvenience.
    """

    def __init__(self, ttl_hours: int = SESSION_HOURS) -> None:
        self._sessions: dict[str, dict] = {}
        self.ttl = timedelta(hours=ttl_hours)

    def create(self, user_id: int, username: str, role: str) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions[token] = {
            "user_id": user_id,
            "username": username,
            "role": role,
            "expires_at": datetime.now() + self.ttl,
        }
        return token

    def get(self, token: str | None) -> dict | None:
        if not token:
            return None
        s = self._sessions.get(token)
        if s is None:
            return None
        if datetime.now() >= s["expires_at"]:
            self._sessions.pop(token, None)
            return None
        return s

    def destroy(self, token: str | None) -> None:
        if token:
            self._sessions.pop(token, None)

    def destroy_for_user(self, user_id: int) -> None:
        """Called when a user is deactivated or their role changes — an
        open session must not outlive the permission that created it."""
        for tok in [
            t for t, s in self._sessions.items() if s["user_id"] == user_id
        ]:
            self._sessions.pop(tok, None)


def authenticate(
    conn: sqlite3.Connection, username: str, password: str
) -> sqlite3.Row:
    """Verify credentials, applying lockout. Raises AuthError in Amharic."""
    from ..i18n.am import t

    row = conn.execute(
        "SELECT * FROM admin_user WHERE username = ? AND active = 1", (username,)
    ).fetchone()

    if row is None:
        # Spend the same time as a real verification so the response time
        # does not reveal whether the username exists.
        hashlib.pbkdf2_hmac("sha256", password.encode(), b"x" * 16, _ITERATIONS)
        raise AuthError(t("auth.wrong"))

    if row["locked_until"]:
        locked_until = datetime.fromisoformat(row["locked_until"])
        if datetime.now() < locked_until:
            raise AuthError(t("auth.locked"))

    if not verify_password(password, row["password_hash"]):
        attempts = row["failed_attempts"] + 1
        lock = (
            (datetime.now() + timedelta(minutes=_LOCK_MINUTES)).isoformat()
            if attempts >= _LOCK_THRESHOLD else None
        )
        conn.execute(
            "UPDATE admin_user SET failed_attempts = ?, locked_until = ? WHERE id = ?",
            (attempts, lock, row["id"]),
        )
        conn.commit()
        raise AuthError(t("auth.locked") if lock else t("auth.wrong"))

    conn.execute(
        "UPDATE admin_user SET failed_attempts = 0, locked_until = NULL, "
        "last_login_at = datetime('now') WHERE id = ?",
        (row["id"],),
    )
    conn.commit()
    return row


def create_user(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    role: Role,
    display_name_am: str = "",
    employee_id: int | None = None,
) -> int:
    if role not in ("viewer", "hr", "admin"):
        raise ValueError(f"unknown role {role!r}")
    cur = conn.execute(
        "INSERT INTO admin_user "
        "(username, display_name_am, password_hash, role, employee_id) "
        "VALUES (?,?,?,?,?)",
        (username, display_name_am, hash_password(password), role, employee_id),
    )
    conn.commit()
    return cur.lastrowid


def change_password(
    conn: sqlite3.Connection, user_id: int, new_password: str
) -> None:
    conn.execute(
        "UPDATE admin_user SET password_hash = ?, must_change_pw = 0 WHERE id = ?",
        (hash_password(new_password), user_id),
    )
    conn.commit()


def hash_pin(pin: str) -> str:
    """
    Employee PINs for the kiosk fallback.

    Short and numeric, so they are hashed with the same KDF rather than a
    plain digest — a four-digit PIN with a fast hash is trivially reversed
    from the database file.
    """
    if not pin.isdigit() or not 4 <= len(pin) <= 8:
        raise ValueError("PIN must be 4 to 8 digits")
    return hash_password(pin + "::pin::sams")


def verify_pin(pin: str, stored: str | None) -> bool:
    if not stored:
        return False
    return verify_password(pin + "::pin::sams", stored)
