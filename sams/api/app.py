"""
የስርዓቱ አገልግሎት — the local FastAPI service.

Binds to 127.0.0.1 only. There is no remote access by design: the machine
is offline and the only clients are the kiosk browser and the admin browser
on the same PC. Building on HTTP anyway means the later "authorized mobile
access" and "multiple stations" items from §12 become a bind-address change
and an auth review rather than a rewrite.
"""

from __future__ import annotations

import base64
import io
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..core.ethiopian import EthiopianDate, month_range, week_range
from ..core.hours import DayStatus, HoursEngine
from ..core.leave import (
    DelegatedTask,
    DelegationPolicy,
    LeaveError,
    LeaveRequest,
    LeaveService,
    LeaveStatus,
    LeaveType,
    TaskStatus,
)
from ..core.workcalendar import DayType
from ..db import repository as repo
from ..db.connection import (
    MonotonicClock,
    backup_to,
    connect,
    init_schema,
    last_verified_backup,
    log_backup,
    record_clock_anomaly,
)
from ..i18n.am import all_strings, t
from ..recognition.engine import FaceGallery, RecognitionUnavailable
from ..reports import builder
from . import auth

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parents[1] / "web"
DATA_DIR = Path.home() / ".sams"


class State:
    """Process-wide state, created once at startup."""

    def __init__(self, db_path: Path) -> None:
        self.conn = connect(db_path)
        init_schema(self.conn)
        self.sessions = auth.SessionStore()
        self.clock = MonotonicClock()
        self.gallery = FaceGallery()
        self.engine: Any = None          # FaceEngine, loaded lazily
        self.image_dir = db_path.parent / "captures"
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.export_dir = db_path.parent / "exports"
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self.reload_gallery()

    def reload_gallery(self) -> None:
        rows = [
            (r["employee_id"], r["vector"])
            for r in self.conn.execute(
                "SELECT fe.employee_id, fe.vector FROM face_embedding fe "
                "JOIN employee e ON e.id = fe.employee_id WHERE e.active = 1"
            )
        ]
        threshold = repo.get_setting(self.conn, "match_threshold", "")
        self.gallery = FaceGallery(
            threshold=float(threshold) if threshold else 0.42,
            min_margin=float(repo.get_setting(self.conn, "min_margin", "0.05")),
        )
        self.gallery.load(rows)
        log.info("gallery loaded: %d embeddings", len(self.gallery))

    def calendar(self):
        return repo.load_calendar(self.conn)

    def hours(self) -> HoursEngine:
        return HoursEngine(self.calendar())

    def leave_service(self) -> LeaveService:
        policy = DelegationPolicy(
            max_credited_days_per_year=int(
                repo.get_setting(self.conn, "max_credited_days", "10")
            ),
            max_concurrent_delegations=int(
                repo.get_setting(self.conn, "max_delegations", "3")
            ),
        )
        return LeaveService(self.calendar(), policy)


state: State | None = None


def get_state() -> State:
    if state is None:
        raise HTTPException(503, "service not started")
    return state


def create_app(db_path: Path | None = None) -> FastAPI:
    global state
    db_path = db_path or (DATA_DIR / "sams.db")
    state = State(db_path)

    app = FastAPI(title=t("app.title"), docs_url=None, redoc_url=None)

    if (WEB_DIR / "static").exists():
        app.mount(
            "/static", StaticFiles(directory=WEB_DIR / "static"), name="static"
        )

    _register_routes(app)
    return app


# -- session helpers -----------------------------------------------------


def current_user(request: Request) -> dict:
    st = get_state()
    token = request.cookies.get("sams_session")
    session = st.sessions.get(token)
    if session is None:
        raise HTTPException(401, t("auth.session_expired"))
    return session


def require(permission: str):
    def dep(user: dict = Depends(current_user)) -> dict:
        if not auth.can(user["role"], permission):
            raise HTTPException(403, t("auth.no_permission"))
        return user

    return dep


# -- request models ------------------------------------------------------


class LoginIn(BaseModel):
    username: str
    password: str


class PunchIn(BaseModel):
    image_b64: str | None = None
    pin: str | None = None
    employee_code: str | None = None


class EmployeeIn(BaseModel):
    employee_code: str
    name_am: str
    name_latin: str = ""
    position_am: str = ""
    phone: str = ""
    department_id: int | None = None
    pin: str | None = None
    hired_on: str | None = None


class LeaveIn(BaseModel):
    employee_id: int
    leave_type: str
    start_date: str
    end_date: str
    reason: str = ""
    hours: float | None = None
    task_description: str | None = None
    task_delegated_to: int | None = None


class DayTypeIn(BaseModel):
    work_date: str
    day_type: str
    note_am: str = ""


class CorrectionIn(BaseModel):
    event_id: int
    captured_at: str
    reason: str


class BulkEntryIn(BaseModel):
    work_date: str
    employee_ids: list[int]
    punch_in: str = "08:30"
    punch_out: str = "17:30"
    reason_code: str = "outage"
    note: str = ""


def _register_routes(app: FastAPI) -> None:  # noqa: C901

    # -- pages ----------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def kiosk_page() -> HTMLResponse:
        return HTMLResponse((WEB_DIR / "kiosk.html").read_text(encoding="utf-8"))

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page() -> HTMLResponse:
        return HTMLResponse((WEB_DIR / "admin.html").read_text(encoding="utf-8"))

    @app.get("/api/strings")
    def strings() -> dict:
        return all_strings()

    # -- auth ------------------------------------------------------------

    @app.post("/api/login")
    def login(body: LoginIn, response: Response) -> dict:
        st = get_state()
        try:
            row = auth.authenticate(st.conn, body.username, body.password)
        except auth.AuthError as e:
            raise HTTPException(401, e.message_am)
        token = st.sessions.create(row["id"], row["username"], row["role"])
        response.set_cookie(
            "sams_session", token, httponly=True, samesite="strict", max_age=28800
        )
        return {
            "username": row["username"],
            "display_name_am": row["display_name_am"],
            "role": row["role"],
            "role_label_am": t(f"role.{row['role']}"),
            "must_change_password": bool(row["must_change_pw"]),
        }

    @app.post("/api/logout")
    def logout(request: Request, response: Response) -> dict:
        get_state().sessions.destroy(request.cookies.get("sams_session"))
        response.delete_cookie("sams_session")
        return {"ok": True}

    @app.get("/api/me")
    def me(user: dict = Depends(current_user)) -> dict:
        return {
            "username": user["username"],
            "role": user["role"],
            "role_label_am": t(f"role.{user['role']}"),
            "permissions": sorted(
                p for p in auth.PERMISSIONS if auth.can(user["role"], p)
            ),
        }

    # -- kiosk -----------------------------------------------------------

    @app.get("/api/kiosk/status")
    def kiosk_status() -> dict:
        st = get_state()
        today = date.today()
        cal = st.calendar()
        dtype = cal.day_type(today)
        eth = EthiopianDate.from_gregorian(today)
        ok, drift, kind = st.clock.check()
        if not ok:
            record_clock_anomaly(
                st.conn, st.clock.expected_wall(), datetime.now(), drift, kind
            )
            st.clock.resync()
        return {
            "open": dtype.kiosk_open,
            "day_type": dtype.value,
            "reason_am": cal.reason_am(today),
            "date_am": eth.format_am(with_weekday=True),
            "time": datetime.now().strftime("%H:%M"),
            "enrolled": len(st.gallery),
            "recognition_available": st.engine is not None,
            "clock_ok": ok,
        }

    @app.post("/api/kiosk/punch")
    def punch(body: PunchIn) -> dict:
        st = get_state()
        today = date.today()
        cal = st.calendar()

        # Validate the request shape first. A malformed call is a client
        # bug and must say so, whatever kind of day it is.
        if not body.image_b64 and not (body.pin and body.employee_code):
            raise HTTPException(400, "image_b64, or pin with employee_code, required")

        if not cal.day_type(today).kiosk_open:
            return {"ok": False, "message_am": t("kiosk.closed_today")}

        employee_id: int | None = None
        method = "face"
        confidence = None
        image_path = None

        if body.pin and body.employee_code:
            row = st.conn.execute(
                "SELECT * FROM employee WHERE employee_code = ? AND active = 1",
                (body.employee_code,),
            ).fetchone()
            if row is None or not auth.verify_pin(body.pin, row["pin_hash"]):
                return {"ok": False, "message_am": t("kiosk.pin_wrong")}
            employee_id, method = row["id"], "pin"

        elif body.image_b64:
            if st.engine is None:
                return {"ok": False, "message_am": t("kiosk.no_camera")}
            try:
                import cv2
                import numpy as np

                raw = base64.b64decode(body.image_b64.split(",")[-1])
                img = cv2.imdecode(
                    np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR
                )
                face, problem = st.engine.single_face(img)
                if face is None:
                    return {"ok": False, "message_am": problem}
                result = st.gallery.match(face.embedding)
                if not result.matched:
                    return {
                        "ok": False,
                        "message_am": t("kiosk.not_recognized"),
                        "offer_pin": True,
                    }
                employee_id = result.employee_id
                confidence = result.similarity
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                p = st.image_dir / f"{employee_id}-{stamp}.jpg"
                cv2.imwrite(str(p), img)
                image_path = str(p)
            except RecognitionUnavailable:
                return {"ok": False, "message_am": t("kiosk.no_camera")}
            except Exception:
                log.exception("punch failed")
                return {"ok": False, "message_am": t("kiosk.system_error")}
        emp = repo.get_employee(st.conn, employee_id)
        if emp is None or not emp["active"]:
            return {"ok": False, "message_am": t("kiosk.inactive")}

        window = int(repo.get_setting(st.conn, "duplicate_window", "60"))
        if repo.recent_event(st.conn, employee_id, window):
            return {
                "ok": False,
                "message_am": t("kiosk.already_punched"),
                "name_am": emp["name_am"],
            }

        now, mono = st.clock.now()
        event_type = repo.next_event_type(st.conn, employee_id, today)
        repo.record_event(
            st.conn, employee_id, event_type, now, mono,
            method=method, confidence=confidence, image_path=image_path,
            work_date=today,
        )
        return {
            "ok": True,
            "name_am": emp["name_am"],
            "event_type": event_type,
            "time": now.strftime("%H:%M"),
            "greeting_am": t(
                "kiosk.welcome_in" if event_type == "in" else "kiosk.welcome_out"
            ),
            "message_am": t(
                "kiosk.recorded_in" if event_type == "in" else "kiosk.recorded_out"
            ),
        }

    # -- dashboard --------------------------------------------------------

    @app.get("/api/dashboard")
    def dashboard(user: dict = Depends(require("view_dashboard"))) -> dict:
        st = get_state()
        today = date.today()
        cal = st.calendar()
        engine = st.hours()
        svc = st.leave_service()
        outages = repo.outage_days(st.conn, today, today)

        # Every employee lands in exactly one bucket, so the cards always
        # sum to the headcount. "other" covers holidays, days off, event
        # days and outages — days nobody owes hours for.
        counts = {"present": 0, "absent": 0, "on_leave": 0,
                  "incomplete": 0, "other": 0}
        rows = []
        for emp in repo.list_employees(st.conn):
            punches = repo.load_punches(st.conn, today, today, emp["id"])
            reqs = repo.list_leave(
                st.conn, emp["id"], LeaveStatus.APPROVED, (today, today)
            )
            lm = svc.leave_days_for_period(reqs, today, today)
            r = engine.compute_day(
                emp["id"], today, punches,
                on_leave=today in lm, leave_covered=lm.get(today, False),
                outage=today in outages,
            )
            if r.status is DayStatus.WORKED:
                counts["present"] += 1
            elif r.status in (DayStatus.ON_LEAVE, DayStatus.LEAVE_COVERED):
                counts["on_leave"] += 1
            elif r.status is DayStatus.INCOMPLETE:
                counts["incomplete"] += 1
            elif r.status is DayStatus.ABSENT:
                counts["absent"] += 1
            else:
                counts["other"] += 1
            rows.append({
                "employee_id": emp["id"],
                "name_am": emp["name_am"],
                "position_am": emp["position_am"],
                "phone": emp["phone"],
                "in": r.punch_in.strftime("%H:%M") if r.punch_in else None,
                "out": r.punch_out.strftime("%H:%M") if r.punch_out else None,
                "worked": round(r.worked_hours, 2),
                "expected": round(r.expected_hours, 2),
                "status": r.status.value,
                "status_am": r.status.label_am,
                "open_tasks": repo.open_task_count(st.conn, emp["id"]),
                "anomalies": r.anomalies,
            })

        backup = last_verified_backup(st.conn)
        backup_stale = True
        if backup and backup["finished_at"]:
            backup_stale = (
                datetime.now() - datetime.fromisoformat(backup["finished_at"])
            ) > timedelta(hours=48)

        eth = EthiopianDate.from_gregorian(today)
        return {
            "date_am": eth.format_am(with_weekday=True),
            "day_type_am": cal.reason_am(today),
            "counts": counts,
            "total_staff": len(rows),
            "rows": rows,
            "upcoming_holidays": [
                {
                    "date_am": EthiopianDate.from_gregorian(d).format_am(),
                    "days_away": (d - today).days,
                    "name_am": name,
                }
                for d, name in cal.upcoming_holidays(today, limit=5)
            ],
            "pending_leave": len(
                repo.list_leave(st.conn, status=LeaveStatus.PENDING)
            ),
            "backup": {
                "stale": backup_stale,
                "last_at": backup["finished_at"] if backup else None,
            },
            "clock_anomalies": st.conn.execute(
                "SELECT COUNT(*) AS n FROM clock_anomaly WHERE acknowledged = 0"
            ).fetchone()["n"],
        }

    # -- availability board ----------------------------------------------

    @app.get("/api/availability")
    def availability(user: dict = Depends(require("view_dashboard"))) -> dict:
        """
        የስራ ክፍፍል — who can take on work today.

        The open-task count is the column that makes this *balancing*
        rather than just a list: without it the same three reliable people
        absorb every reassignment.
        """
        st = get_state()
        today = date.today()
        engine = st.hours()
        svc = st.leave_service()
        out = []
        for emp in repo.list_employees(st.conn):
            punches = repo.load_punches(st.conn, today, today, emp["id"])
            reqs = repo.list_leave(
                st.conn, emp["id"], LeaveStatus.APPROVED, (today, today)
            )
            lm = svc.leave_days_for_period(reqs, today, today)
            r = engine.compute_day(
                emp["id"], today, punches,
                on_leave=today in lm, leave_covered=lm.get(today, False),
            )
            out.append({
                "employee_id": emp["id"],
                "name_am": emp["name_am"],
                "position_am": emp["position_am"],
                "phone": emp["phone"],
                "status_am": r.status.label_am,
                "available": r.status is DayStatus.WORKED,
                "open_tasks": repo.open_task_count(st.conn, emp["id"]),
            })
        out.sort(key=lambda x: (not x["available"], x["open_tasks"]))
        return {"date_am": EthiopianDate.from_gregorian(today).format_am(),
                "rows": out}

    # -- employees --------------------------------------------------------

    @app.get("/api/employees")
    def employees(
        include_inactive: bool = False,
        user: dict = Depends(require("view_employees")),
    ) -> list[dict]:
        st = get_state()
        return [
            {
                "id": r["id"], "employee_code": r["employee_code"],
                "name_am": r["name_am"], "name_latin": r["name_latin"],
                "position_am": r["position_am"], "phone": r["phone"],
                "department_am": r["department_am"], "active": bool(r["active"]),
                "faces": st.conn.execute(
                    "SELECT COUNT(*) AS n FROM face_embedding WHERE employee_id = ?",
                    (r["id"],),
                ).fetchone()["n"],
            }
            for r in repo.list_employees(st.conn, active_only=not include_inactive)
        ]

    @app.post("/api/employees")
    def add_employee(
        body: EmployeeIn, user: dict = Depends(require("manage_employees"))
    ) -> dict:
        st = get_state()
        data = body.model_dump(exclude={"pin"})
        if body.pin:
            data["pin_hash"] = auth.hash_pin(body.pin)
        try:
            new_id = repo.create_employee(st.conn, data, user["user_id"])
        except Exception as e:
            raise HTTPException(400, str(e))
        return {"id": new_id}

    @app.delete("/api/employees/{employee_id}")
    def deactivate(
        employee_id: int, user: dict = Depends(require("manage_employees"))
    ) -> dict:
        st = get_state()
        repo.deactivate_employee(st.conn, employee_id, user["user_id"])
        st.reload_gallery()
        return {"ok": True}

    # -- attendance -------------------------------------------------------

    @app.get("/api/attendance/{employee_id}")
    def attendance(
        employee_id: int, start: str, end: str,
        user: dict = Depends(require("view_dashboard")),
    ) -> dict:
        st = get_state()
        return builder.individual_history(
            st.conn, employee_id, *_range(start, end, date.today())
        )

    @app.post("/api/attendance/correct")
    def correct(
        body: CorrectionIn, user: dict = Depends(require("correct_records"))
    ) -> dict:
        st = get_state()
        new_id = repo.supersede_event(
            st.conn, body.event_id,
            {"captured_at": datetime.fromisoformat(body.captured_at)},
            user["user_id"], body.reason,
        )
        return {"new_event_id": new_id}

    @app.post("/api/attendance/bulk")
    def bulk_entry(
        body: BulkEntryIn, user: dict = Depends(require("bulk_entry"))
    ) -> dict:
        """
        Enter a day's attendance from the paper register after a full-day
        outage. Every row is marked `manual` with a reason code, so the
        exception report shows exactly what was entered by hand.
        """
        st = get_state()
        day = _parse_date(body.work_date)
        h_in, m_in = (int(x) for x in body.punch_in.split(":"))
        h_out, m_out = (int(x) for x in body.punch_out.split(":"))
        n = 0
        for emp_id in body.employee_ids:
            repo.record_event(
                st.conn, emp_id, "in",
                datetime.combine(day, datetime.min.time()).replace(
                    hour=h_in, minute=m_in),
                0, method="manual", work_date=day,
                created_by=user["user_id"], reason_code=body.reason_code,
                note=body.note,
            )
            repo.record_event(
                st.conn, emp_id, "out",
                datetime.combine(day, datetime.min.time()).replace(
                    hour=h_out, minute=m_out),
                0, method="manual", work_date=day,
                created_by=user["user_id"], reason_code=body.reason_code,
                note=body.note,
            )
            n += 1
        return {"entered": n}

    # -- leave -------------------------------------------------------------

    @app.get("/api/leave")
    def leave_list(
        status: str | None = None, user: dict = Depends(require("view_own_leave"))
    ) -> list[dict]:
        st = get_state()
        reqs = repo.list_leave(
            st.conn, status=LeaveStatus(status) if status else None
        )
        out = []
        for r in reqs:
            emp = repo.get_employee(st.conn, r.employee_id)
            out.append({
                "id": r.id,
                "employee_id": r.employee_id,
                "name_am": emp["name_am"] if emp else "—",
                "leave_type_am": r.leave_type.label_am,
                "start_am": EthiopianDate.from_gregorian(r.start_date).format_short_am(),
                "end_am": EthiopianDate.from_gregorian(r.end_date).format_short_am(),
                "days": len(r.working_days(st.calendar())),
                "status": r.status.value,
                "status_am": r.status.label_am,
                "reason": r.reason,
                "task": None if r.task is None else {
                    "id": r.task.id,
                    "description": r.task.description,
                    "delegated_to": r.task.delegated_to,
                    "status_am": r.task.status.label_am,
                    "complete": r.task.is_complete,
                },
            })
        return out

    @app.get("/api/leave/balance/{employee_id}")
    def leave_balance(
        employee_id: int, user: dict = Depends(require("view_own_leave"))
    ) -> dict:
        st = get_state()
        eth_year = LeaveService.balance_year_for(
            date.today(),
            int(repo.get_setting(st.conn, "leave_reset_month", "11")),
        )
        bal = repo.load_balance(st.conn, employee_id, eth_year)
        return {"eth_year": eth_year, "rows": bal.summary_am()}

    @app.post("/api/leave")
    def request_leave(
        body: LeaveIn, user: dict = Depends(require("view_own_leave"))
    ) -> dict:
        st = get_state()
        svc = st.leave_service()
        try:
            task = None
            if body.task_description and body.task_delegated_to:
                task = DelegatedTask(
                    id=None, leave_request_id=0,
                    description=body.task_description,
                    delegated_to=body.task_delegated_to,
                    delegated_by=body.employee_id,
                    covers_from=_parse_date(body.start_date, "የመጀመሪያ ቀን"),
                    covers_to=_parse_date(body.end_date, "የመጨረሻ ቀን"),
                )
            req = LeaveRequest(
                id=None, employee_id=body.employee_id,
                leave_type=LeaveType(body.leave_type),
                start_date=_parse_date(body.start_date, "የመጀመሪያ ቀን"),
                end_date=_parse_date(body.end_date, "የመጨረሻ ቀን"),
                reason=body.reason, hours=body.hours, task=task,
            )
            eth_year = LeaveService.balance_year_for(req.start_date)
            bal = repo.load_balance(st.conn, body.employee_id, eth_year)
            existing = repo.list_leave(st.conn, body.employee_id)
            open_tasks = (
                repo.open_task_count(st.conn, body.task_delegated_to)
                if body.task_delegated_to else 0
            )
            svc.validate(req, bal, existing, delegate_open_tasks=open_tasks)
            new_id = repo.create_leave(st.conn, req, task)
        except LeaveError as e:
            raise HTTPException(400, e.message_am)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"id": new_id}

    @app.post("/api/leave/{leave_id}/decide")
    def decide(
        leave_id: int, approve: bool, note: str = "",
        user: dict = Depends(require("decide_leave")),
    ) -> dict:
        st = get_state()
        repo.decide_leave(st.conn, leave_id, approve, user["user_id"], note)
        return {"ok": True}

    @app.post("/api/task/{task_id}/confirm")
    def confirm_task(
        task_id: int, completed: bool, note: str = "",
        user: dict = Depends(require("confirm_tasks")),
    ) -> dict:
        """Confirming a delegated task is what turns a leave day into a paid
        one, so it is always an administrative action with a named actor."""
        st = get_state()
        repo.confirm_task(st.conn, task_id, completed, user["user_id"], note)
        return {"ok": True}

    # -- calendar ----------------------------------------------------------

    @app.get("/api/calendar")
    def calendar_month(
        eth_year: int, eth_month: int,
        user: dict = Depends(require("view_dashboard")),
    ) -> dict:
        st = get_state()
        cal = st.calendar()
        start, end = month_range(eth_year, eth_month)
        from ..core.ethiopian import iter_days

        days = []
        for d in iter_days(start, end):
            eth = EthiopianDate.from_gregorian(d)
            dt = cal.day_type(d)
            days.append({
                "gregorian": d.isoformat(),
                "eth_day": eth.day,
                "weekday_am": eth.weekday_name_am,
                "day_type": dt.value,
                "day_type_am": dt.label_am,
                "reason_am": cal.reason_am(d),
                "expected_hours": cal.expected_hours(d),
            })
        return {
            "month_am": EthiopianDate(eth_year, eth_month, 1).month_name_am,
            "eth_year": eth_year, "eth_month": eth_month, "days": days,
        }

    @app.post("/api/calendar/day")
    def set_day(
        body: DayTypeIn, user: dict = Depends(require("manage_calendar"))
    ) -> dict:
        st = get_state()
        repo.set_day_override(
            st.conn, _parse_date(body.work_date),
            DayType(body.day_type), body.note_am, user["user_id"],
        )
        return {"ok": True}

    @app.delete("/api/calendar/day/{work_date}")
    def clear_day(
        work_date: str, user: dict = Depends(require("manage_calendar"))
    ) -> dict:
        st = get_state()
        repo.clear_day_override(
            st.conn, _parse_date(work_date), user["user_id"]
        )
        return {"ok": True}

    # -- reports -----------------------------------------------------------

    @app.get("/api/reports/{kind}")
    def report(
        kind: str,
        start: str | None = None, end: str | None = None,
        eth_year: int | None = None, eth_month: int | None = None,
        employee_id: int | None = None,
        user: dict = Depends(require("view_reports")),
    ) -> dict:
        st = get_state()
        today = date.today()
        eth_today = EthiopianDate.from_gregorian(today)

        if kind == "daily":
            return builder.daily_register(
                st.conn, _parse_date(start) if start else today
            )
        if kind == "monthly":
            return builder.monthly_summary(
                st.conn, eth_year or eth_today.year, eth_month or eth_today.month
            )
        if kind == "individual":
            if employee_id is None:
                raise HTTPException(400, "employee_id required")
            s, e = _range(start, end, today)
            return builder.individual_history(st.conn, employee_id, s, e)
        if kind in ("absence", "exception", "tasks"):
            s, e = _range(start, end, today)
            fn = {
                "absence": builder.absence_report,
                "exception": builder.exception_report,
                "tasks": builder.task_distribution,
            }[kind]
            return fn(st.conn, s, e)
        if kind == "leave":
            return builder.leave_summary(st.conn, eth_year or eth_today.year)
        raise HTTPException(404, f"unknown report {kind}")

    @app.get("/api/reports/{kind}/export")
    def export_report(
        kind: str, fmt: str = "pdf",
        start: str | None = None, end: str | None = None,
        eth_year: int | None = None, eth_month: int | None = None,
        employee_id: int | None = None,
        user: dict = Depends(require("view_reports")),
    ) -> FileResponse:
        from ..reports.export import to_excel, to_pdf

        st = get_state()
        data = report(kind, start, end, eth_year, eth_month, employee_id, user)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        if fmt == "excel":
            path = to_excel(data, st.export_dir / f"{kind}-{stamp}.xlsx")
            media = (
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            )
        else:
            path = to_pdf(data, st.export_dir / f"{kind}-{stamp}.pdf")
            media = "application/pdf"
        return FileResponse(path, media_type=media, filename=path.name)

    # -- admin -------------------------------------------------------------

    @app.post("/api/admin/backup")
    def run_backup(
        destination: str | None = None,
        user: dict = Depends(require("run_backup")),
    ) -> dict:
        st = get_state()
        dest = Path(destination) if destination else (
            DATA_DIR / "backups" / f"sams-{datetime.now():%Y%m%d-%H%M%S}.db"
        )
        try:
            result = backup_to(st.conn, dest)
            log_backup(st.conn, result)
            return {"ok": True, **result}
        except Exception as e:
            raise HTTPException(500, f"ምትኬ አልተሳካም: {e}")

    @app.post("/api/admin/calibrate")
    def calibrate(user: dict = Depends(require("calibrate"))) -> dict:
        """
        Recalibrate the match threshold on the enrolled staff.

        A false-reject rate above ~2% means enrollment quality is the
        problem. Re-enroll; never lower the threshold to hide it.
        """
        st = get_state()
        st.reload_gallery()
        stats = st.gallery.calibrate()
        repo.set_setting(
            st.conn, "match_threshold", str(stats["threshold"]), user["user_id"]
        )
        st.reload_gallery()
        stats["advice_am"] = (
            "የመለያ ጥራት ጥሩ ነው።" if stats.get("false_reject_rate", 0) <= 0.02
            else "የፎቶ ጥራት ዝቅተኛ ነው። ሰራተኞችን እንደገና ይመዝግቡ።"
        )
        return stats

    @app.get("/api/admin/audit")
    def audit_log(
        limit: int = 200, user: dict = Depends(require("manage_users"))
    ) -> list[dict]:
        st = get_state()
        return [
            dict(r) for r in st.conn.execute(
                "SELECT a.*, u.username FROM audit_log a "
                "LEFT JOIN admin_user u ON u.id = a.admin_user_id "
                "ORDER BY a.id DESC LIMIT ?", (limit,)
            )
        ]

    @app.get("/api/health")
    def health() -> dict:
        st = get_state()
        from ..db.connection import integrity_check, verify_hash_chain

        broken = verify_hash_chain(st.conn)
        return {
            "database_ok": integrity_check(st.conn),
            "hash_chain_ok": not broken,
            "broken_events": broken[:20],
            "enrolled_embeddings": len(st.gallery),
            "recognition_loaded": st.engine is not None,
            "clock_ok": st.clock.check()[0],
        }


def _parse_date(value: str, field: str = "ቀን") -> date:
    """
    Parse an ISO date or fail with a clean 400.

    A malformed date must never reach date.fromisoformat unguarded: an
    uncaught ValueError becomes a 500 with a stack trace, which both looks
    like a broken system to HR and leaks internals to anyone poking at the
    service.
    """
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise HTTPException(400, f"የተሳሳተ {field} ቅርጸት: {value!r}")


def _range(start: str | None, end: str | None, today: date) -> tuple[date, date]:
    if start and end:
        s, e = _parse_date(start, "የመጀመሪያ ቀን"), _parse_date(end, "የመጨረሻ ቀን")
        if e < s:
            raise HTTPException(400, "የመጨረሻ ቀን ከመጀመሪያ ቀን ሊቀድም አይችልም")
        return s, e
    return week_range(today)
