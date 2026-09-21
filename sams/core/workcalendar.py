"""
የስራ ቀን መቁጠሪያ — the work calendar.

Decides, for any date, what kind of day it is and how many hours are expected.
Everything HR can adjust lives here: the weekly pattern, public holidays,
event days, and one-off overrides.

Design rule: expected hours for a date are DERIVED, never stored against an
employee. One edit by HR to a day's type corrects every affected figure at
once, and reports recompute rather than carrying stale totals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from enum import Enum
from typing import Iterable


class DayType(str, Enum):
    """What kind of day this is. Drives expected hours and kiosk availability."""

    FULL = "full"           # ሙሉ የስራ ቀን
    HALF_MORNING = "half_am"  # ግማሽ ቀን (ጥዋት)
    HALF_AFTERNOON = "half_pm"  # ግማሽ ቀን (ከሰዓት)
    OFF = "off"             # የእረፍት ቀን
    HOLIDAY = "holiday"     # የህዝብ በዓል
    EVENT = "event"         # የመርሃ ግብር ቀን — all staff credited

    @property
    def label_am(self) -> str:
        return {
            DayType.FULL: "ሙሉ የስራ ቀን",
            DayType.HALF_MORNING: "ግማሽ ቀን (ጥዋት)",
            DayType.HALF_AFTERNOON: "ግማሽ ቀን (ከሰዓት)",
            DayType.OFF: "የእረፍት ቀን",
            DayType.HOLIDAY: "የህዝብ በዓል",
            DayType.EVENT: "የመርሃ ግብር ቀን",
        }[self]

    @property
    def is_working(self) -> bool:
        return self in (
            DayType.FULL,
            DayType.HALF_MORNING,
            DayType.HALF_AFTERNOON,
            DayType.EVENT,
        )

    @property
    def kiosk_open(self) -> bool:
        """Whether the attendance station accepts punches."""
        return self in (
            DayType.FULL,
            DayType.HALF_MORNING,
            DayType.HALF_AFTERNOON,
        )


@dataclass(frozen=True)
class Shift:
    """The office's daily working periods."""

    morning_start: time = time(8, 30)
    morning_end: time = time(12, 30)
    afternoon_start: time = time(14, 0)
    afternoon_end: time = time(17, 30)

    def __post_init__(self) -> None:
        if not (
            self.morning_start
            < self.morning_end
            <= self.afternoon_start
            < self.afternoon_end
        ):
            raise ValueError(
                "shift periods must be ordered: "
                "morning_start < morning_end <= afternoon_start < afternoon_end"
            )

    @property
    def morning_hours(self) -> float:
        return _hours_between(self.morning_start, self.morning_end)

    @property
    def afternoon_hours(self) -> float:
        return _hours_between(self.afternoon_start, self.afternoon_end)

    @property
    def full_day_hours(self) -> float:
        return self.morning_hours + self.afternoon_hours

    @property
    def lunch_window(self) -> tuple[time, time]:
        return self.morning_end, self.afternoon_start

    def expected_for(self, day_type: DayType) -> float:
        if day_type in (DayType.FULL, DayType.EVENT):
            return self.full_day_hours
        if day_type is DayType.HALF_MORNING:
            return self.morning_hours
        if day_type is DayType.HALF_AFTERNOON:
            return self.afternoon_hours
        return 0.0


def _hours_between(a: time, b: time) -> float:
    return (
        (b.hour * 60 + b.minute + b.second / 60)
        - (a.hour * 60 + a.minute + a.second / 60)
    ) / 60.0


@dataclass
class Holiday:
    """
    A public holiday or office event.

    Fixed-date holidays are defined either in Gregorian terms (Labour Day,
    Adwa) or Ethiopian terms (እንቁጣጣሽ, መስቀል, ገና, ጥምቀት). Movable holidays —
    ፋሲካ and the Islamic holidays — cannot be computed years ahead and are
    entered by HR each year as explicit dates.
    """

    name_am: str
    day_type: DayType = DayType.HOLIDAY
    # exactly one of these three addressing modes is used
    gregorian_month_day: tuple[int, int] | None = None
    ethiopian_month_day: tuple[int, int] | None = None
    explicit_date: date | None = None
    note_am: str = ""

    def __post_init__(self) -> None:
        modes = [
            self.gregorian_month_day,
            self.ethiopian_month_day,
            self.explicit_date,
        ]
        if sum(m is not None for m in modes) != 1:
            raise ValueError(
                f"holiday {self.name_am!r} must use exactly one of "
                "gregorian_month_day, ethiopian_month_day, explicit_date"
            )

    def falls_on(self, g: date) -> bool:
        from .ethiopian import EthiopianDate

        if self.explicit_date is not None:
            return g == self.explicit_date
        if self.gregorian_month_day is not None:
            return (g.month, g.day) == self.gregorian_month_day
        eth = EthiopianDate.from_gregorian(g)
        return (eth.month, eth.day) == self.ethiopian_month_day


# Fixed public holidays. Movable ones (ፋሲካ, ስቅለት, ዒድ አል ፈጥር, ዒድ አል አድሃ,
# መውሊድ) are deliberately absent — HR enters those each year, because the
# lunar and computus dates cannot be hard-coded safely years ahead.
DEFAULT_HOLIDAYS: tuple[Holiday, ...] = (
    Holiday("እንቁጣጣሽ (አዲስ ዓመት)", ethiopian_month_day=(1, 1)),
    Holiday("መስቀል", ethiopian_month_day=(1, 17)),
    Holiday("ገና", ethiopian_month_day=(4, 29)),
    Holiday("ጥምቀት", ethiopian_month_day=(5, 11)),
    Holiday("የአድዋ ድል በዓል", gregorian_month_day=(3, 2)),
    Holiday("የሰራተኞች ቀን", gregorian_month_day=(5, 1)),
    Holiday("የአርበኞች ቀን", gregorian_month_day=(5, 5)),
    Holiday("ደርግ የወደቀበት ቀን", gregorian_month_day=(5, 28)),
)


@dataclass
class WorkCalendar:
    """
    Resolves any date to a DayType.

    Precedence, highest first:
      1. an explicit override for that exact date (HR set it by hand)
      2. a holiday
      3. the weekly pattern
    """

    shift: Shift = field(default_factory=Shift)
    # Monday=0 .. Sunday=6
    weekly_pattern: dict[int, DayType] = field(
        default_factory=lambda: {
            0: DayType.FULL,
            1: DayType.FULL,
            2: DayType.FULL,
            3: DayType.FULL,
            4: DayType.FULL,
            5: DayType.OFF,  # ቅዳሜ — HR can switch to FULL or HALF_MORNING
            6: DayType.OFF,  # እሁድ
        }
    )
    holidays: list[Holiday] = field(
        default_factory=lambda: list(DEFAULT_HOLIDAYS)
    )
    overrides: dict[date, DayType] = field(default_factory=dict)
    override_notes: dict[date, str] = field(default_factory=dict)

    def set_override(
        self, g: date, day_type: DayType, note_am: str = ""
    ) -> None:
        """HR marking a single date — an event, an unplanned closure, a
        working Saturday. Takes precedence over everything else."""
        self.overrides[g] = day_type
        if note_am:
            self.override_notes[g] = note_am

    def clear_override(self, g: date) -> None:
        self.overrides.pop(g, None)
        self.override_notes.pop(g, None)

    def holiday_on(self, g: date) -> Holiday | None:
        for h in self.holidays:
            if h.falls_on(g):
                return h
        return None

    def day_type(self, g: date) -> DayType:
        if g in self.overrides:
            return self.overrides[g]
        h = self.holiday_on(g)
        if h is not None:
            return h.day_type
        return self.weekly_pattern.get(g.weekday(), DayType.OFF)

    def expected_hours(self, g: date) -> float:
        return self.shift.expected_for(self.day_type(g))

    def reason_am(self, g: date) -> str:
        """Why the kiosk is closed, or what kind of day this is — shown on screen."""
        if g in self.override_notes:
            return self.override_notes[g]
        h = self.holiday_on(g)
        if h is not None and g not in self.overrides:
            return h.name_am
        return self.day_type(g).label_am

    def working_days(self, start: date, end: date) -> list[date]:
        from .ethiopian import iter_days

        return [d for d in iter_days(start, end) if self.day_type(d).is_working]

    def expected_total(self, start: date, end: date) -> float:
        from .ethiopian import iter_days

        return round(sum(self.expected_hours(d) for d in iter_days(start, end)), 4)

    def upcoming_holidays(
        self, from_date: date, limit: int = 5, horizon_days: int = 400
    ) -> list[tuple[date, str]]:
        """For the dashboard — the next few holidays and events."""
        from datetime import timedelta

        found: list[tuple[date, str]] = []
        cur = from_date
        stop = from_date + timedelta(days=horizon_days)
        while cur <= stop and len(found) < limit:
            dt = self.day_type(cur)
            if dt in (DayType.HOLIDAY, DayType.EVENT):
                found.append((cur, self.reason_am(cur)))
            cur += timedelta(days=1)
        return found


def resolve_saturday(calendar: WorkCalendar, day_type: DayType) -> None:
    """Convenience for HR: set what Saturday means, office-wide."""
    if day_type not in (
        DayType.FULL,
        DayType.HALF_MORNING,
        DayType.HALF_AFTERNOON,
        DayType.OFF,
    ):
        raise ValueError("Saturday must be full, half, or off")
    calendar.weekly_pattern[5] = day_type
