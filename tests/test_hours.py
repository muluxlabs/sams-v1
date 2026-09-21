"""
Destructive tests for the worked-hours engine.

This is the module payroll depends on, so the tests are written to break it:
messy punches, missing punches, midnight spans, holidays, half days, and the
lunch-deduction edge cases that a flat deduction would get wrong.
"""

from datetime import date, datetime, time

import pytest

from sams.core.hours import DayStatus, HoursEngine, Punch, overlap_minutes
from sams.core.workcalendar import DayType, Shift, WorkCalendar

# A plain Monday, a full working day under the default calendar.
MON = date(2026, 9, 21)
# Deliberately NOT 26/27 September 2026: that Sunday is መስቀል (መስከረም 17),
# so it resolves to HOLIDAY rather than DAY_OFF. Kept as its own test below.
SAT = date(2026, 10, 3)
SUN = date(2026, 10, 4)


def dt(d: date, h: int, m: int = 0) -> datetime:
    return datetime.combine(d, time(h, m))


def punches(d: date, in_h, in_m, out_h, out_m, method="face", emp=1):
    return [
        Punch(emp, dt(d, in_h, in_m), "in", method),
        Punch(emp, dt(d, out_h, out_m), "out", method),
    ]


@pytest.fixture
def engine():
    return HoursEngine(WorkCalendar())


# -- the shift itself ---------------------------------------------------


def test_default_shift_is_seven_and_a_half_hours():
    s = Shift()
    assert s.morning_hours == 4.0       # 08:30-12:30
    assert s.afternoon_hours == 3.5     # 14:00-17:30
    assert s.full_day_hours == 7.5


def test_shift_rejects_out_of_order_periods():
    with pytest.raises(ValueError):
        Shift(morning_start=time(13, 0), morning_end=time(12, 0))
    with pytest.raises(ValueError):
        Shift(morning_end=time(15, 0), afternoon_start=time(14, 0))


# -- lunch deduction: the rule most systems get wrong --------------------


def test_full_day_deducts_the_whole_lunch_window(engine):
    r = engine.compute_day(1, MON, punches(MON, 8, 30, 17, 30))
    assert r.worked_hours == 7.5
    assert r.lunch_deducted == 1.5
    assert r.difference == 0.0


def test_morning_only_loses_nothing_to_lunch(engine):
    """
    The case a flat 1.5-hour deduction gets wrong. Someone who works
    08:30-12:30 never touched the lunch window and must be credited the
    full 4 hours, not 2.5.
    """
    r = engine.compute_day(1, MON, punches(MON, 8, 30, 12, 30))
    assert r.lunch_deducted == 0.0
    assert r.worked_hours == 4.0
    assert r.difference == -3.5


def test_partial_lunch_overlap_deducts_only_the_overlap(engine):
    """Left at 13:00 — inside lunch by 30 minutes, so lose 30 minutes only."""
    r = engine.compute_day(1, MON, punches(MON, 8, 30, 13, 0))
    assert r.lunch_deducted == 0.5
    assert r.worked_hours == 4.0


def test_afternoon_only_loses_nothing_to_lunch(engine):
    r = engine.compute_day(1, MON, punches(MON, 14, 0, 17, 30))
    assert r.lunch_deducted == 0.0
    assert r.worked_hours == 3.5


def test_arriving_during_lunch_deducts_only_the_remainder(engine):
    r = engine.compute_day(1, MON, punches(MON, 13, 0, 17, 30))
    assert r.lunch_deducted == 1.0   # 13:00-14:00
    assert r.worked_hours == 3.5


def test_entirely_inside_lunch_window_is_zero_worked(engine):
    r = engine.compute_day(1, MON, punches(MON, 12, 45, 13, 45))
    assert r.worked_hours == 0.0


@pytest.mark.parametrize(
    "in_h,in_m,out_h,out_m,expected",
    [
        (9, 0, 17, 30, 7.0),     # an hour late, full afternoon
        (8, 30, 14, 0, 4.0),     # left at the start of the afternoon
        (8, 30, 16, 0, 6.0),     # left early
        (7, 0, 18, 30, 10.0),    # long day, overtime visible
        (8, 30, 12, 0, 3.5),     # left before lunch
        (12, 0, 14, 30, 1.0),    # straddles lunch entirely
    ],
)
def test_worked_hours_table(engine, in_h, in_m, out_h, out_m, expected):
    r = engine.compute_day(1, MON, punches(MON, in_h, in_m, out_h, out_m))
    assert r.worked_hours == pytest.approx(expected)


def test_overlap_helper_is_symmetric_and_clamped():
    a, b = time(12, 30), time(14, 0)
    assert overlap_minutes(dt(MON, 8), dt(MON, 12), a, b) == 0
    assert overlap_minutes(dt(MON, 8), dt(MON, 18), a, b) == 90
    assert overlap_minutes(dt(MON, 13), dt(MON, 13, 30), a, b) == 30
    assert overlap_minutes(dt(MON, 15), dt(MON, 16), a, b) == 0


# -- no lateness anywhere ------------------------------------------------


def test_there_is_no_late_status(engine):
    """The office accounts for hours; it does not label people late."""
    assert not hasattr(DayStatus, "LATE")
    r = engine.compute_day(1, MON, punches(MON, 11, 0, 17, 30))
    assert r.status is DayStatus.WORKED     # not "late"
    assert r.worked_hours == 5.0            # the shortfall shows as hours
    assert r.difference == -2.5


# -- messy punches -------------------------------------------------------


def test_missing_check_out_is_incomplete_not_paid(engine):
    r = engine.compute_day(1, MON, [Punch(1, dt(MON, 8, 30), "in")])
    assert r.status is DayStatus.INCOMPLETE
    assert r.worked_hours == 0.0
    assert "የመውጫ ምልክት የለም" in r.anomalies


def test_missing_check_in_is_incomplete(engine):
    r = engine.compute_day(1, MON, [Punch(1, dt(MON, 17, 30), "out")])
    assert r.status is DayStatus.INCOMPLETE
    assert "የመግቢያ ምልክት የለም" in r.anomalies


def test_first_in_last_out_wins_with_repeated_punches(engine):
    ps = [
        Punch(1, dt(MON, 8, 30), "in"),
        Punch(1, dt(MON, 8, 31), "in"),
        Punch(1, dt(MON, 12, 30), "out"),
        Punch(1, dt(MON, 14, 0), "in"),
        Punch(1, dt(MON, 17, 30), "out"),
    ]
    r = engine.compute_day(1, MON, ps)
    assert r.punch_in == dt(MON, 8, 30)
    assert r.punch_out == dt(MON, 17, 30)
    assert r.worked_hours == 7.5
    assert r.anomalies  # flagged, not silently swallowed


def test_out_before_in_is_refused_not_negative(engine):
    ps = [Punch(1, dt(MON, 17, 0), "in"), Punch(1, dt(MON, 8, 0), "out")]
    r = engine.compute_day(1, MON, ps)
    assert r.status is DayStatus.INCOMPLETE
    assert r.worked_hours == 0.0
    assert r.worked_hours >= 0


def test_implausibly_long_span_is_flagged_not_paid(engine):
    """A forgotten check-out must never pay out 18 hours."""
    ps = [Punch(1, dt(MON, 5, 30), "in"), Punch(1, dt(MON, 23, 59), "out")]
    r = engine.compute_day(1, MON, ps)
    assert r.status is DayStatus.INCOMPLETE
    assert r.worked_hours == 0.0
    assert any("የማይታመን" in a for a in r.anomalies)


def test_span_just_under_the_limit_is_still_paid(engine):
    """The threshold must not swallow a genuinely long day."""
    r = engine.compute_day(1, MON, punches(MON, 7, 0, 21, 0))
    assert r.status is DayStatus.WORKED
    assert r.worked_hours == 12.5


def test_holiday_falling_on_a_weekend_is_a_holiday():
    """
    መስቀል fell on a Sunday in 2026. It must resolve as HOLIDAY, and leave
    taken across it must not consume balance — covered in the leave tests.
    """
    cal = WorkCalendar()
    meskel_sunday = date(2026, 9, 27)
    assert meskel_sunday.weekday() == 6
    assert cal.day_type(meskel_sunday) is DayType.HOLIDAY
    assert cal.expected_hours(meskel_sunday) == 0.0


def test_worked_hours_are_never_negative(engine):
    for in_h in range(0, 23):
        for out_h in range(in_h, 24):
            r = engine.compute_day(1, MON, punches(MON, in_h, 0, out_h, 0))
            assert r.worked_hours >= 0.0


def test_no_punches_is_absent(engine):
    r = engine.compute_day(1, MON, [])
    assert r.status is DayStatus.ABSENT
    assert r.worked_hours == 0.0
    assert r.expected_hours == 7.5


def test_punches_from_another_day_are_ignored(engine):
    other = [Punch(1, dt(date(2026, 9, 22), 8, 30), "in")]
    r = engine.compute_day(1, MON, other)
    assert r.status is DayStatus.ABSENT


# -- day types -----------------------------------------------------------


def test_weekend_expects_nothing(engine):
    for d in (SAT, SUN):
        r = engine.compute_day(1, d, [])
        assert r.status is DayStatus.DAY_OFF
        assert r.expected_hours == 0.0
        assert r.difference == 0.0


def test_punching_on_a_day_off_is_recorded_but_not_paid(engine):
    r = engine.compute_day(1, SAT, punches(SAT, 8, 30, 17, 30))
    assert r.status is DayStatus.DAY_OFF
    assert r.credited_hours == 0.0
    assert r.notes_am


def test_half_day_expects_half(engine):
    cal = WorkCalendar()
    cal.set_override(MON, DayType.HALF_MORNING, "የሰራተኞች ስብሰባ")
    e = HoursEngine(cal)
    r = e.compute_day(1, MON, punches(MON, 8, 30, 12, 30))
    assert r.expected_hours == 4.0
    assert r.worked_hours == 4.0
    assert r.difference == 0.0


def test_event_day_credits_everyone_without_punches(engine):
    cal = WorkCalendar()
    cal.set_override(MON, DayType.EVENT, "የልማት መርሃ ግብር")
    e = HoursEngine(cal)
    r = e.compute_day(1, MON, [])
    assert r.status is DayStatus.EVENT
    assert r.credited_hours == 7.5
    assert r.difference == 0.0


def test_holiday_expects_nothing_and_closes_the_kiosk():
    cal = WorkCalendar()
    genna = date(2026, 1, 7)
    assert cal.day_type(genna) is DayType.HOLIDAY
    assert not cal.day_type(genna).kiosk_open
    assert cal.expected_hours(genna) == 0.0
    assert "ገና" in cal.reason_am(genna)


def test_override_beats_holiday_and_pattern():
    cal = WorkCalendar()
    genna = date(2026, 1, 7)
    cal.set_override(genna, DayType.FULL, "አስቸኳይ የስራ ቀን")
    assert cal.day_type(genna) is DayType.FULL
    assert cal.expected_hours(genna) == 7.5
    cal.clear_override(genna)
    assert cal.day_type(genna) is DayType.HOLIDAY


def test_saturday_can_be_switched_on():
    from sams.core.workcalendar import resolve_saturday

    cal = WorkCalendar()
    assert cal.expected_hours(SAT) == 0.0
    resolve_saturday(cal, DayType.HALF_MORNING)
    assert cal.expected_hours(SAT) == 4.0
    resolve_saturday(cal, DayType.FULL)
    assert cal.expected_hours(SAT) == 7.5


def test_outage_day_is_distinguished_from_absence(engine):
    r = engine.compute_day(1, MON, [], outage=True)
    assert r.status is DayStatus.OUTAGE
    assert r.status is not DayStatus.ABSENT


# -- leave interaction ---------------------------------------------------


def test_approved_leave_is_paid_expected_hours(engine):
    r = engine.compute_day(1, MON, [], on_leave=True)
    assert r.status is DayStatus.ON_LEAVE
    assert r.worked_hours == 0.0
    assert r.credited_hours == 7.5
    assert r.status.is_credited


def test_covered_leave_is_marked_distinctly(engine):
    r = engine.compute_day(1, MON, [], on_leave=True, leave_covered=True)
    assert r.status is DayStatus.LEAVE_COVERED
    assert r.credited_hours == 7.5


def test_leave_outranks_a_stray_punch(engine):
    r = engine.compute_day(1, MON, punches(MON, 8, 30, 9, 0), on_leave=True)
    assert r.status is DayStatus.ON_LEAVE


# -- period aggregation --------------------------------------------------


def test_a_full_working_week_balances(engine):
    start, end = date(2026, 9, 21), date(2026, 9, 27)  # Mon-Sun
    ps = []
    for i in range(5):
        d = date(2026, 9, 21 + i)
        ps += punches(d, 8, 30, 17, 30)
    p = engine.compute_period(1, start, end, ps)
    assert p.expected_hours == 37.5
    assert p.worked_hours == 37.5
    assert p.difference == 0.0
    assert p.completion_ratio == 1.0


def test_short_week_shows_the_shortfall(engine):
    start, end = date(2026, 9, 21), date(2026, 9, 25)
    ps = []
    for i in range(5):
        d = date(2026, 9, 21 + i)
        # 09:30-17:00 is a 7.5h span less the full 1.5h lunch = 6.0h worked
        ps += punches(d, 9, 30, 17, 0)
    p = engine.compute_period(1, start, end, ps)
    assert p.worked_hours == 30.0
    assert p.expected_hours == 37.5
    assert p.difference == -7.5
    assert p.completion_ratio == pytest.approx(30.0 / 37.5)


def test_period_totals_equal_the_sum_of_their_days(engine):
    """Every total must expand to its days, or disputes cannot be settled."""
    start, end = date(2026, 9, 1), date(2026, 9, 30)
    ps = []
    for i in range(30):
        d = date(2026, 9, 1) + (date(2026, 9, 2) - date(2026, 9, 1)) * i
        ps += punches(d, 8, 30, 16, 0)
    p = engine.compute_period(1, start, end, ps)
    assert p.worked_hours == pytest.approx(
        round(sum(x.worked_hours for x in p.days), 2)
    )
    assert p.expected_hours == pytest.approx(
        round(sum(x.expected_hours for x in p.days), 2)
    )


def test_completion_ratio_is_one_when_nothing_expected(engine):
    """A period of only weekends must not divide by zero."""
    p = engine.compute_period(1, SAT, SUN, [])
    assert p.expected_hours == 0.0
    assert p.completion_ratio == 1.0


def test_other_employees_punches_are_ignored(engine):
    ps = punches(MON, 8, 30, 17, 30, emp=2)
    p = engine.compute_period(1, MON, MON, ps)
    assert p.days[0].status is DayStatus.ABSENT


def test_pin_and_manual_methods_are_surfaced(engine):
    r = engine.compute_day(1, MON, punches(MON, 8, 30, 17, 30, method="pin"))
    assert "pin" in r.methods
    assert any("ፒን" in n for n in r.notes_am)


def test_punch_rejects_unknown_kind_and_method():
    with pytest.raises(ValueError):
        Punch(1, dt(MON, 8), "sideways")
    with pytest.raises(ValueError):
        Punch(1, dt(MON, 8), "in", method="telepathy")
