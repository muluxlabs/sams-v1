"""
End-to-end tests against the running service.

Focus is the security boundary rather than the happy path: role escalation,
session handling, brute force, injection reaching the browser, and the kiosk
refusing to record on a closed day.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from sams.api import app as app_module
from sams.api import auth
from sams.core.workcalendar import DayType
from sams.db import repository as repo


@pytest.fixture
def client(tmp_path):
    app = app_module.create_app(tmp_path / "api.db")
    st = app_module.state
    conn = st.conn
    conn.execute(
        "INSERT OR IGNORE INTO department (id, name_am) VALUES (1, 'ጽህፈት ቤት')"
    )
    conn.commit()
    auth.create_user(conn, "admin", "adminpass123", "admin", "አስተዳዳሪ")
    auth.create_user(conn, "hr", "hrpass12345", "hr", "የሰው ሃይል")
    auth.create_user(conn, "viewer", "viewpass123", "viewer", "ተመልካች")
    repo.create_employee(conn, {
        "employee_code": "W-001", "name_am": "አበበ ከበደ",
        "position_am": "ጸሐፊ", "phone": "0911000000",
        "pin_hash": auth.hash_pin("1234"), "department_id": 1,
    })
    with TestClient(app) as c:
        yield c


def login(client, user="admin", pw="adminpass123"):
    r = client.post("/api/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return r.json()


# -- auth ----------------------------------------------------------------


def test_login_succeeds_and_reports_the_amharic_role(client):
    d = login(client)
    assert d["role"] == "admin"
    assert d["role_label_am"] == "አስተዳዳሪ"


def test_wrong_password_is_refused_in_amharic(client):
    r = client.post("/api/login", json={"username": "admin", "password": "nope"})
    assert r.status_code == 401
    assert any("ሀ" <= ch <= "፿" for ch in r.json()["detail"])


def test_unknown_user_is_refused_the_same_way(client):
    r = client.post("/api/login", json={"username": "ghost", "password": "x"})
    assert r.status_code == 401


def test_brute_force_locks_the_account(client):
    for _ in range(5):
        client.post("/api/login", json={"username": "hr", "password": "wrong"})
    r = client.post("/api/login", json={"username": "hr", "password": "hrpass12345"})
    assert r.status_code == 401
    assert "ተቆልፏል" in r.json()["detail"]


def test_protected_endpoints_refuse_anonymous_callers(client):
    for path in ("/api/dashboard", "/api/employees", "/api/leave",
                 "/api/availability", "/api/admin/audit"):
        assert client.get(path).status_code == 401, path


def test_logout_invalidates_the_session(client):
    login(client)
    assert client.get("/api/dashboard").status_code == 200
    client.post("/api/logout")
    assert client.get("/api/dashboard").status_code == 401


def test_a_forged_session_cookie_is_rejected(client):
    client.cookies.set("sams_session", "not-a-real-token")
    assert client.get("/api/dashboard").status_code == 401


# -- role escalation ------------------------------------------------------


def test_viewer_cannot_create_employees(client):
    login(client, "viewer", "viewpass123")
    r = client.post("/api/employees", json={
        "employee_code": "W-900", "name_am": "አዲስ"})
    assert r.status_code == 403
    assert "ፈቃድ" in r.json()["detail"]


def test_viewer_cannot_reach_admin_functions(client):
    login(client, "viewer", "viewpass123")
    assert client.post("/api/admin/backup").status_code == 403
    assert client.post("/api/admin/calibrate").status_code == 403
    assert client.get("/api/admin/audit").status_code == 403


def test_hr_cannot_manage_users_or_settings(client):
    login(client, "hr", "hrpass12345")
    assert client.get("/api/admin/audit").status_code == 403
    assert client.post("/api/admin/backup").status_code == 403


def test_hr_can_do_hr_work(client):
    login(client, "hr", "hrpass12345")
    assert client.get("/api/dashboard").status_code == 200
    r = client.post("/api/employees", json={
        "employee_code": "W-002", "name_am": "ሰላም ተክሌ"})
    assert r.status_code == 200


def test_viewer_can_still_read_reports(client):
    login(client, "viewer", "viewpass123")
    assert client.get("/api/reports/daily").status_code == 200


def test_permissions_list_matches_the_role(client):
    login(client, "viewer", "viewpass123")
    perms = client.get("/api/me").json()["permissions"]
    assert "view_reports" in perms
    assert "manage_users" not in perms
    assert "correct_records" not in perms


# -- kiosk ----------------------------------------------------------------


def test_kiosk_status_is_public_and_amharic(client):
    d = client.get("/api/kiosk/status").json()
    assert "date_am" in d
    assert any("ሀ" <= ch <= "፿" for ch in d["date_am"])


def test_pin_punch_records_an_event(client):
    st = app_module.state
    repo.set_day_override(st.conn, date.today(), DayType.FULL, "የሙከራ ቀን", None)
    r = client.post("/api/kiosk/punch",
                    json={"pin": "1234", "employee_code": "W-001"}).json()
    assert r["ok"] is True
    assert r["event_type"] == "in"
    assert r["name_am"] == "አበበ ከበደ"


def test_second_punch_is_an_exit(client):
    st = app_module.state
    repo.set_day_override(st.conn, date.today(), DayType.FULL, "የሙከራ ቀን", None)
    client.post("/api/kiosk/punch", json={"pin": "1234", "employee_code": "W-001"})
    repo.set_setting(st.conn, "duplicate_window", "0")
    r = client.post("/api/kiosk/punch",
                    json={"pin": "1234", "employee_code": "W-001"}).json()
    assert r["ok"] is True and r["event_type"] == "out"


def test_duplicate_punch_inside_the_window_is_suppressed(client):
    st = app_module.state
    repo.set_day_override(st.conn, date.today(), DayType.FULL, "የሙከራ ቀን", None)
    client.post("/api/kiosk/punch", json={"pin": "1234", "employee_code": "W-001"})
    r = client.post("/api/kiosk/punch",
                    json={"pin": "1234", "employee_code": "W-001"}).json()
    assert r["ok"] is False
    assert "ቀደም ብለው" in r["message_am"]


def test_wrong_pin_is_refused(client):
    st = app_module.state
    repo.set_day_override(st.conn, date.today(), DayType.FULL, "የሙከራ ቀን", None)
    r = client.post("/api/kiosk/punch",
                    json={"pin": "9999", "employee_code": "W-001"}).json()
    assert r["ok"] is False
    assert "ፒን" in r["message_am"]


def test_kiosk_refuses_on_a_holiday(client):
    st = app_module.state
    repo.set_day_override(st.conn, date.today(), DayType.HOLIDAY, "ገና", None)
    assert client.get("/api/kiosk/status").json()["open"] is False
    r = client.post("/api/kiosk/punch",
                    json={"pin": "1234", "employee_code": "W-001"}).json()
    assert r["ok"] is False
    assert "የስራ ቀን አይደለም" in r["message_am"]


def test_deactivated_employee_cannot_punch(client):
    st = app_module.state
    repo.set_day_override(st.conn, date.today(), DayType.FULL, "የሙከራ ቀን", None)
    repo.deactivate_employee(st.conn, 1, None)
    r = client.post("/api/kiosk/punch",
                    json={"pin": "1234", "employee_code": "W-001"}).json()
    assert r["ok"] is False


def test_punch_without_pin_or_image_is_a_bad_request(client):
    assert client.post("/api/kiosk/punch", json={}).status_code == 400


# -- leave ----------------------------------------------------------------


def test_leave_request_and_approval_round_trip(client):
    login(client)
    monday = date.today() + timedelta(days=(7 - date.today().weekday()))
    r = client.post("/api/leave", json={
        "employee_id": 1, "leave_type": "annual",
        "start_date": monday.isoformat(), "end_date": monday.isoformat(),
        "reason": "የግል ጉዳይ"})
    assert r.status_code == 200, r.text
    leave_id = r.json()["id"]
    assert client.post(f"/api/leave/{leave_id}/decide?approve=true").status_code == 200
    rows = client.get("/api/leave").json()
    assert any(x["id"] == leave_id and x["status"] == "approved" for x in rows)


def test_leave_beyond_balance_is_refused_in_amharic(client):
    login(client)
    start = date.today() + timedelta(days=7)
    r = client.post("/api/leave", json={
        "employee_id": 1, "leave_type": "annual",
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=60)).isoformat()})
    assert r.status_code == 400
    assert any("ሀ" <= ch <= "፿" for ch in r.json()["detail"])


def test_delegating_to_yourself_is_refused(client):
    login(client)
    monday = date.today() + timedelta(days=(7 - date.today().weekday()))
    r = client.post("/api/leave", json={
        "employee_id": 1, "leave_type": "annual",
        "start_date": monday.isoformat(), "end_date": monday.isoformat(),
        "task_description": "ስራ", "task_delegated_to": 1})
    assert r.status_code == 400


def test_leave_balance_is_reported(client):
    login(client)
    d = client.get("/api/leave/balance/1").json()
    assert d["rows"]
    assert d["rows"][0]["የፈቃድ ዓይነት"]


# -- dashboard and availability -------------------------------------------


def test_dashboard_counts_reconcile_with_headcount(client):
    login(client)
    d = client.get("/api/dashboard").json()
    assert sum(d["counts"].values()) == d["total_staff"]


def test_dashboard_flags_a_missing_backup(client):
    login(client)
    assert client.get("/api/dashboard").json()["backup"]["stale"] is True


def test_availability_sorts_available_staff_first(client):
    login(client)
    rows = client.get("/api/availability").json()["rows"]
    availability = [r["available"] for r in rows]
    assert availability == sorted(availability, reverse=True)


# -- reports ---------------------------------------------------------------


def test_every_report_kind_responds(client):
    login(client)
    for kind in ("daily", "monthly", "absence", "leave", "exception", "tasks"):
        r = client.get(f"/api/reports/{kind}")
        assert r.status_code == 200, kind
        assert r.json()["title_am"]


def test_unknown_report_is_a_404(client):
    login(client)
    assert client.get("/api/reports/nonsense").status_code == 404


def test_pdf_export_returns_a_pdf(client):
    login(client)
    r = client.get("/api/reports/daily/export?fmt=pdf")
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")


def test_excel_export_returns_a_workbook(client):
    login(client)
    r = client.get("/api/reports/monthly/export?fmt=excel")
    assert r.status_code == 200
    assert r.content[:2] == b"PK"


# -- hostile input ---------------------------------------------------------


def test_script_tag_in_a_name_survives_as_data(client):
    """
    The API stores and returns the payload literally; escaping is the
    template's job. This asserts no server-side mangling and no execution
    path — the admin UI escapes on render.
    """
    login(client)
    payload = "አበበ<script>alert(1)</script>"
    client.post("/api/employees", json={
        "employee_code": "W-XSS", "name_am": payload})
    names = [e["name_am"] for e in client.get("/api/employees").json()]
    assert payload in names


def test_sql_injection_in_a_report_parameter_is_harmless(client):
    login(client)
    r = client.get("/api/reports/daily?start=2026-01-01'; DROP TABLE employee; --")
    assert r.status_code in (200, 400, 422)
    assert client.get("/api/employees").status_code == 200


def test_malformed_date_does_not_crash_the_service(client):
    login(client)
    r = client.get("/api/reports/individual?employee_id=1&start=not-a-date&end=x")
    assert r.status_code in (400, 422, 500)
    assert client.get("/api/dashboard").status_code == 200


def test_duplicate_employee_code_returns_a_clean_error(client):
    login(client)
    body = {"employee_code": "W-001", "name_am": "ሌላ ሰው"}
    assert client.post("/api/employees", json=body).status_code == 400


def test_health_endpoint_reports_integrity(client):
    d = client.get("/api/health").json()
    assert d["database_ok"] is True
    assert d["hash_chain_ok"] is True


# -- pages -----------------------------------------------------------------


def test_kiosk_and_admin_pages_render_amharic(client):
    for path in ("/", "/admin"):
        html = client.get(path).text
        assert any("ሀ" <= ch <= "፿" for ch in html), path
        assert 'lang="am"' in html
