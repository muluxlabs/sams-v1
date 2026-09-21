"""
የስራ ሰዓት ስሌት — the worked-hours engine.

This is the module payroll depends on, so it is written to be boring,
total, and auditable. Three rules govern it:

  1. Nothing is stored. Every figure is recomputed from raw punch events,
     so a correction to an event or to the work calendar propagates
     everywhere immediately and no stale total can survive.

  2. Lunch is deducted by OVERLAP, never as a flat amount. Someone who
     works only the morning never touched the lunch window and must not
     lose time for it. A flat deduction quietly steals 1.5 hours from
     every morning-only day.

  3. Every computed figure carries the reasons behind it, so a disputed
     payroll row can be expanded down to the punches that produced it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import Enum

from .workcalendar import DayType, Shift, WorkCalendar


class DayStatus(str, Enum):
    """
    The outcome for one employee on one day.

    Note what is absent: there is no LATE. The office does not label
    lateness; it accounts for hours. Arriving at 09:00 simply produces
    fewer worked hours than expected, which the report shows plainly.
    """

    WORKED = "worked"               # ሰርቷል
    ON_LEAVE = "on_leave"           # በፈቃድ ላይ
    LEAVE_COVERED = "leave_covered"  # በፈቃድ ላይ — ተግባር ተሰጥቷል
    HOLIDAY = "holiday"             # የህዝብ በዓል
    DAY_OFF = "day_off"             # የእረፍት ቀን
    EVENT = "event"                 # የመርሃ ግብር ቀን
    ABSENT = "absent"               # አልተገኘም
    INCOMPLETE = "incomplete"       # ያልተሟላ መዝገብ
    OUTAGE = "outage"               # የኤሌክትሪክ መቋረጥ

    @property
    def label_am(self) -> str:
        return {
            DayStatus.WORKED: "ሰርቷል",
            DayStatus.ON_LEAVE: "በፈቃድ ላይ",
            DayStatus.LEAVE_COVERED: "በፈቃድ ላይ (ተግባር ተሰጥቷል)",
            DayStatus.HOLIDAY: "የህዝብ በዓል",
            DayStatus.DAY_OFF: "የእረፍት ቀን",
            DayStatus.EVENT: "የመርሃ ግብር ቀን",
            DayStatus.ABSENT: "አልተገኘም",
            DayStatus.INCOMPLETE: "ያልተሟላ መዝገብ",
            DayStatus.OUTAGE: "የኤሌክትሪክ መቋረጥ",
        }[self]

    @property
    def is_credited(self) -> bool:
        """Whether the day counts as fulfilled for payroll purposes."""
        return self in (
            DayStatus.ON_LEAVE,
            DayStatus.LEAVE_COVERED,
            DayStatus.EVENT,
        )


@dataclass(frozen=True)
class Punch:
    """One raw attendance event, exactly as captured."""

    employee_id: int
    at: datetime
    kind: str            # "in" | "out"
    method: str = "face"  # "face" | "pin" | "manual"
    event_id: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("in", "out"):
            raise ValueError(f"kind must be 'in' or 'out', got {self.kind!r}")
        if self.method not in ("face", "pin", "manual"):
            raise ValueError(f"unknown method {self.method!r}")


@dataclass
class DayResult:
    """One employee, one day, fully explained."""

    employee_id: int
    day: date
    status: DayStatus
    expected_hours: float
    worked_hours: float
    punch_in: datetime | None = None
    punch_out: datetime | None = None
    lunch_deducted: float = 0.0
    methods: tuple[str, ...] = ()
    notes_am: list[str] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)

    @property
    def difference(self) -> float:
        """
        Shortfall against the expectation. Negative means short.

        Measured on CREDITED hours, not worked hours, so an approved leave
        day or an event day shows no shortfall — nobody owes time for a day
        the office settled. For an ordinary working day credited equals
        worked, so this is the plain difference.
        """
        return round(self.credited_hours - self.expected_hours, 4)

    @property
    def credited_hours(self) -> float:
        """
        Hours counted for pay.

        A credited status (approved leave, a covered leave day, an event
        day) pays the expected hours without punches. Otherwise pay
        follows the hours actually worked.
        """
        if self.status.is_credited:
            return self.expected_hours
        return self.worked_hours

    def to_row(self) -> dict:
        from .ethiopian import EthiopianDate

        eth = EthiopianDate.from_gregorian(self.day)
        return {
            "ቀን": eth.format_short_am(),
            "ዕለት": eth.weekday_name_am,
            "የመግቢያ ሰዓት": _fmt(self.punch_in),
            "የመውጫ ሰዓት": _fmt(self.punch_out),
            "የሰራው ሰዓት": round(self.worked_hours, 2),
            "የሚጠበቅ ሰዓት": round(self.expected_hours, 2),
            "ልዩነት": self.difference,
            "ሁኔታ": self.status.label_am,
        }


def _fmt(dt: datetime | None) -> str:
    return dt.strftime("%H:%M") if dt else "—"


def _to_minutes(t: time) -> int:
    return t.hour * 60 + t.minute + (1 if t.second >= 30 else 0)


def overlap_minutes(
    start_a: datetime, end_a: datetime, window_start: time, window_end: time
) -> int:
    """
    Minutes of [start_a, end_a] that fall inside the daily window
    [window_start, window_end]. Used for the lunch deduction.

    Handles the case where the punch span crosses midnight by clamping to
    the day of the opening punch — a span that long is flagged as an
    anomaly elsewhere and should not silently deduct multiple lunches.
    """
    day = start_a.date()
    w_start = datetime.combine(day, window_start)
    w_end = datetime.combine(day, window_end)
    lo = max(start_a, w_start)
    hi = min(end_a, w_end)
    if hi <= lo:
        return 0
    return int((hi - lo).total_seconds() // 60)


class HoursEngine:
    """Computes worked hours from raw punches against the work calendar."""

    # A span longer than this is almost certainly a missed check-out
    # rather than genuine work, and is flagged rather than paid.
    MAX_PLAUSIBLE_SPAN_HOURS = 16.0

    # Two punches of the same kind within this many seconds are treated
    # as one person tapping twice, not as two events.
    DUPLICATE_WINDOW_SECONDS = 60

    def __init__(self, calendar: WorkCalendar | None = None) -> None:
        self.calendar = calendar or WorkCalendar()

    @property
    def shift(self) -> Shift:
        return self.calendar.shift

    # -- punch selection ----------------------------------------------

    def _select_punches(
        self, punches: list[Punch]
    ) -> tuple[Punch | None, Punch | None, list[str]]:
        """
        Reduce a day's raw punches to one in and one out.

        Real behaviour at a kiosk is messy: people tap twice, forget to
        check out, or check out and come back. The rule is first-in,
        last-out, which is the reading most favourable to an honest
        employee and the one HR can explain to anyone who asks.
        """
        anomalies: list[str] = []
        ins = sorted([p for p in punches if p.kind == "in"], key=lambda p: p.at)
        outs = sorted([p for p in punches if p.kind == "out"], key=lambda p: p.at)

        if len(ins) > 1:
            anomalies.append(f"{len(ins)} የመግቢያ ምልክቶች ተመዝግበዋል")
        if len(outs) > 1:
            anomalies.append(f"{len(outs)} የመውጫ ምልክቶች ተመዝግበዋል")

        first_in = ins[0] if ins else None
        last_out = outs[-1] if outs else None

        if first_in and last_out and last_out.at < first_in.at:
            anomalies.append("የመውጫ ሰዓት ከመግቢያ ሰዓት ይቀድማል")
            return first_in, None, anomalies

        return first_in, last_out, anomalies

    # -- the single-day computation -----------------------------------

    def compute_day(
        self,
        employee_id: int,
        day: date,
        punches: list[Punch],
        *,
        on_leave: bool = False,
        leave_covered: bool = False,
        outage: bool = False,
    ) -> DayResult:
        day_type = self.calendar.day_type(day)
        expected = self.calendar.expected_hours(day)
        same_day = [p for p in punches if p.at.date() == day]

        def result(status: DayStatus, worked: float = 0.0, **kw) -> DayResult:
            return DayResult(
                employee_id=employee_id,
                day=day,
                status=status,
                expected_hours=expected,
                worked_hours=worked,
                **kw,
            )

        # Non-working days first. Punches on them are recorded but not paid;
        # the office does not owe hours on a holiday and does not credit
        # unrequested work without an explicit overtime decision.
        if day_type is DayType.HOLIDAY:
            r = result(DayStatus.HOLIDAY)
            if same_day:
                r.notes_am.append("በበዓል ቀን ምልክት ተደርጓል")
            return r
        if day_type is DayType.OFF:
            r = result(DayStatus.DAY_OFF)
            if same_day:
                r.notes_am.append("በእረፍት ቀን ምልክት ተደርጓል")
            return r
        if day_type is DayType.EVENT:
            # worked stays 0: nobody punched, and "hours worked" must mean
            # hours actually at the desk. The day is settled through
            # credited_hours instead, exactly as an approved leave day is.
            # Putting expected into worked here would inflate the worked
            # column and make it disagree with the sum of the punches.
            r = result(DayStatus.EVENT, worked=0.0)
            r.notes_am.append(self.calendar.reason_am(day))
            return r

        # Leave outranks punches: an approved leave day is settled whether
        # or not someone happened to pass the kiosk.
        if on_leave:
            status = DayStatus.LEAVE_COVERED if leave_covered else DayStatus.ON_LEAVE
            r = result(status, worked=0.0)
            if leave_covered:
                r.notes_am.append("ተግባሩ ለሌላ ሰራተኛ ተሰጥቷል")
            return r

        if outage and not same_day:
            r = result(DayStatus.OUTAGE)
            r.notes_am.append("በኤሌክትሪክ መቋረጥ ምክንያት አልተመዘገበም")
            return r

        if not same_day:
            return result(DayStatus.ABSENT)

        first_in, last_out, anomalies = self._select_punches(same_day)

        if first_in is None:
            r = result(DayStatus.INCOMPLETE, punch_out=last_out.at if last_out else None)
            r.anomalies = anomalies + ["የመግቢያ ምልክት የለም"]
            return r

        if last_out is None:
            r = result(DayStatus.INCOMPLETE, punch_in=first_in.at)
            r.anomalies = anomalies + ["የመውጫ ምልክት የለም"]
            r.methods = (first_in.method,)
            return r

        span_hours = (last_out.at - first_in.at).total_seconds() / 3600.0

        if span_hours > self.MAX_PLAUSIBLE_SPAN_HOURS:
            r = result(
                DayStatus.INCOMPLETE,
                punch_in=first_in.at,
                punch_out=last_out.at,
            )
            r.anomalies = anomalies + [
                f"የማይታመን የቆይታ ጊዜ ({span_hours:.1f} ሰዓት)"
            ]
            r.methods = (first_in.method, last_out.method)
            return r

        lunch_start, lunch_end = self.shift.lunch_window
        lunch_min = overlap_minutes(first_in.at, last_out.at, lunch_start, lunch_end)
        worked = max(0.0, span_hours - lunch_min / 60.0)

        r = result(
            DayStatus.WORKED,
            worked=round(worked, 4),
            punch_in=first_in.at,
            punch_out=last_out.at,
            lunch_deducted=round(lunch_min / 60.0, 4),
        )
        r.anomalies = anomalies
        r.methods = (first_in.method, last_out.method)
        if "pin" in r.methods:
            r.notes_am.append("በፒን ኮድ ተመዝግቧል")
        if "manual" in r.methods:
            r.notes_am.append("በእጅ የገባ መዝገብ")
        return r

    # -- period aggregation -------------------------------------------

    def compute_period(
        self,
        employee_id: int,
        start: date,
        end: date,
        punches: list[Punch],
        *,
        leave_days: dict[date, bool] | None = None,
        outage_days: set[date] | None = None,
    ) -> "PeriodResult":
        """
        leave_days maps a date to whether the leave was covered by a
        delegated task. outage_days marks dates the station was down.
        """
        from .ethiopian import iter_days

        leave_days = leave_days or {}
        outage_days = outage_days or set()

        by_day: dict[date, list[Punch]] = {}
        for p in punches:
            if p.employee_id != employee_id:
                continue
            by_day.setdefault(p.at.date(), []).append(p)

        days = [
            self.compute_day(
                employee_id,
                d,
                by_day.get(d, []),
                on_leave=d in leave_days,
                leave_covered=leave_days.get(d, False),
                outage=d in outage_days,
            )
            for d in iter_days(start, end)
        ]
        return PeriodResult(employee_id=employee_id, start=start, end=end, days=days)


@dataclass
class PeriodResult:
    """An employee's account for a period. Every total expands to its days."""

    employee_id: int
    start: date
    end: date
    days: list[DayResult]

    @property
    def expected_hours(self) -> float:
        return round(sum(d.expected_hours for d in self.days), 2)

    @property
    def worked_hours(self) -> float:
        return round(sum(d.worked_hours for d in self.days), 2)

    @property
    def credited_hours(self) -> float:
        return round(sum(d.credited_hours for d in self.days), 2)

    @property
    def difference(self) -> float:
        return round(self.credited_hours - self.expected_hours, 2)

    @property
    def completion_ratio(self) -> float:
        """Credited over expected. This is what payroll multiplies by."""
        if self.expected_hours <= 0:
            return 1.0
        return round(self.credited_hours / self.expected_hours, 6)

    def count(self, status: DayStatus) -> int:
        return sum(1 for d in self.days if d.status is status)

    @property
    def anomalous_days(self) -> list[DayResult]:
        return [d for d in self.days if d.anomalies]

    def summary_am(self) -> dict:
        return {
            "የሚጠበቅ ሰዓት": self.expected_hours,
            "የሰራው ሰዓት": self.worked_hours,
            "የተከፈለ ሰዓት": self.credited_hours,
            "ልዩነት": self.difference,
            "የሰራባቸው ቀናት": self.count(DayStatus.WORKED),
            "በፈቃድ ላይ": self.count(DayStatus.ON_LEAVE)
            + self.count(DayStatus.LEAVE_COVERED),
            "ያልተገኘባቸው ቀናት": self.count(DayStatus.ABSENT),
            "ያልተሟሉ መዝገቦች": self.count(DayStatus.INCOMPLETE),
        }
