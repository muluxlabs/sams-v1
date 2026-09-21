"""
Destructive tests for leave, balances and the delegation-credit rule.

The delegation rule — hand your task to a colleague and your leave day is
paid as worked — is the most gameable piece of logic in the system, so most
of this file is spent trying to game it.
"""

from datetime import date, datetime

import pytest

from sams.core.hours import DayStatus, HoursEngine
from sams.core.leave import (
    DEFAULT_ENTITLEMENTS,
    DelegatedTask,
    DelegationPolicy,
    LeaveBalance,
    LeaveError,
    LeaveRequest,
    LeaveService,
    LeaveStatus,
    LeaveType,
    TaskStatus,
)
from sams.core.workcalendar import DayType, WorkCalendar

MON = date(2026, 9, 21)
FRI = date(2026, 9, 25)


@pytest.fixture
def svc():
    return LeaveService(WorkCalendar(), DelegationPolicy())


@pytest.fixture
def balance():
    return LeaveBalance(
        employee_id=1, eth_year=2019, entitlements=dict(DEFAULT_ENTITLEMENTS)
    )


def make_request(**kw) -> LeaveRequest:
    base = dict(
        id=1,
        employee_id=1,
        leave_type=LeaveType.ANNUAL,
        start_date=MON,
        end_date=MON,
        requested_at=datetime(2026, 9, 15, 10, 0),
    )
    base.update(kw)
    return LeaveRequest(**base)


def make_task(**kw) -> DelegatedTask:
    base = dict(
        id=1,
        leave_request_id=1,
        description="የደንበኛ ፋይሎችን ማደራጀት",
        delegated_to=2,
        delegated_by=1,
        covers_from=MON,
        covers_to=MON,
    )
    base.update(kw)
    return DelegatedTask(**base)


# -- basic validity ------------------------------------------------------


def test_end_before_start_is_refused():
    with pytest.raises(LeaveError):
        make_request(start_date=FRI, end_date=MON)


def test_short_leave_requires_hours():
    with pytest.raises(LeaveError):
        make_request(leave_type=LeaveType.SHORT, hours=None)


def test_leave_errors_carry_an_amharic_message():
    try:
        make_request(start_date=FRI, end_date=MON)
    except LeaveError as e:
        assert e.message_am
        assert any("ሀ" <= ch <= "፿" for ch in e.message_am)


# -- weekends and holidays must not consume balance ----------------------


def test_leave_spanning_a_weekend_only_costs_working_days(svc):
    """Mon-Fri plus the weekend = 5 days of balance, not 7."""
    req = make_request(start_date=MON, end_date=date(2026, 9, 27))
    assert len(req.dates()) == 7
    assert req.balance_cost(svc.calendar) == 5.0


def test_leave_spanning_a_holiday_skips_it(svc):
    """ገና is 7 January 2026, a Wednesday. Leave across it costs 4, not 5."""
    genna = date(2026, 1, 7)
    assert svc.calendar.day_type(genna) is DayType.HOLIDAY
    req = make_request(start_date=date(2026, 1, 5), end_date=date(2026, 1, 9))
    assert len(req.working_days(svc.calendar)) == 4


def test_official_duty_never_consumes_balance(svc):
    req = make_request(leave_type=LeaveType.OFFICIAL_DUTY, end_date=FRI)
    assert req.balance_cost(svc.calendar) == 0.0
    assert not LeaveType.OFFICIAL_DUTY.consumes_balance


def test_weekend_only_request_is_refused(svc, balance):
    req = make_request(start_date=date(2026, 10, 3), end_date=date(2026, 10, 4))
    with pytest.raises(LeaveError):
        svc.validate(req, balance, [])


# -- balances ------------------------------------------------------------


def test_balance_starts_at_the_entitlement(balance):
    assert balance.remaining(LeaveType.ANNUAL) == 10.0


def test_cannot_exceed_the_balance(svc, balance):
    balance.used[LeaveType.ANNUAL] = 8.0
    req = make_request(start_date=MON, end_date=FRI)  # 5 working days
    with pytest.raises(LeaveError) as e:
        svc.validate(req, balance, [])
    assert "ቀሪ" in e.value.message_am or "የቀረዎት" in e.value.message_am


def test_exactly_the_remaining_balance_is_allowed(svc, balance):
    balance.used[LeaveType.ANNUAL] = 5.0
    req = make_request(start_date=MON, end_date=FRI)
    svc.validate(req, balance, [])  # must not raise


def test_balance_is_consumed_on_approval(svc, balance):
    req = make_request(start_date=MON, end_date=FRI, status=LeaveStatus.APPROVED)
    svc.apply_to_balance(req, balance)
    assert balance.remaining(LeaveType.ANNUAL) == 5.0


def test_cancelling_returns_the_balance(svc, balance):
    req = make_request(start_date=MON, end_date=FRI, status=LeaveStatus.APPROVED)
    svc.apply_to_balance(req, balance)
    svc.release_balance(req, balance)
    assert balance.remaining(LeaveType.ANNUAL) == 10.0


def test_balance_never_goes_negative_on_release(svc, balance):
    req = make_request(start_date=MON, end_date=FRI, status=LeaveStatus.APPROVED)
    svc.release_balance(req, balance)
    assert balance.taken(LeaveType.ANNUAL) == 0.0


def test_pending_requests_do_not_consume_balance(svc, balance):
    req = make_request(status=LeaveStatus.PENDING)
    svc.apply_to_balance(req, balance)
    assert balance.remaining(LeaveType.ANNUAL) == 10.0


def test_balance_year_resets_on_hamle_by_default():
    # ሐምሌ 1, 2019 EC is in July 2027
    assert LeaveService.balance_year_for(date(2027, 7, 10)) == 2019
    assert LeaveService.balance_year_for(date(2027, 6, 10)) == 2018
    # …and on መስከረም if HR configures it that way
    assert LeaveService.balance_year_for(date(2026, 9, 20), reset_month=1) == 2019


# -- overlaps ------------------------------------------------------------


def test_overlapping_request_is_refused(svc, balance):
    existing = [
        make_request(id=9, start_date=MON, end_date=FRI, status=LeaveStatus.APPROVED)
    ]
    new = make_request(id=10, start_date=date(2026, 9, 23), end_date=date(2026, 9, 24))
    with pytest.raises(LeaveError):
        svc.validate(new, balance, existing)


def test_rejected_leave_does_not_block_a_new_request(svc, balance):
    existing = [
        make_request(id=9, start_date=MON, end_date=FRI, status=LeaveStatus.REJECTED)
    ]
    new = make_request(id=10, start_date=MON, end_date=MON)
    svc.validate(new, balance, existing)


def test_another_employees_leave_does_not_block(svc, balance):
    existing = [
        make_request(
            id=9, employee_id=2, start_date=MON, end_date=FRI,
            status=LeaveStatus.APPROVED,
        )
    ]
    svc.validate(make_request(id=10), balance, existing)


def test_editing_the_same_request_does_not_self_block(svc, balance):
    existing = [make_request(id=1, status=LeaveStatus.APPROVED)]
    svc.validate(make_request(id=1), balance, existing)


# -- the delegation rule: trying to break it -----------------------------


def test_delegating_to_yourself_is_refused():
    with pytest.raises(LeaveError):
        make_task(delegated_to=1, delegated_by=1)


def test_plain_approved_leave_is_not_credited_as_worked(svc):
    req = make_request(status=LeaveStatus.APPROVED)
    assert svc.credited_days(req) == {MON: False}


def test_delegated_and_completed_leave_is_credited(svc):
    task = make_task(
        status=TaskStatus.COMPLETED, confirmed_by=3,
        confirmed_at=datetime(2026, 9, 22, 9, 0),
    )
    req = make_request(status=LeaveStatus.APPROVED, task=task)
    assert svc.credited_days(req) == {MON: True}


def test_delegated_but_unfinished_is_not_credited(svc):
    """Credit follows completion, not the promise of it."""
    req = make_request(
        status=LeaveStatus.APPROVED, task=make_task(status=TaskStatus.ASSIGNED)
    )
    assert svc.credited_days(req) == {MON: False}


def test_task_marked_not_done_is_not_credited(svc):
    req = make_request(
        status=LeaveStatus.APPROVED, task=make_task(status=TaskStatus.NOT_DONE)
    )
    assert svc.credited_days(req) == {MON: False}


def test_confirming_your_own_cover_is_not_credited(svc):
    """
    The obvious abuse: go on leave, 'delegate' to a colleague, then confirm
    the work yourself. Must not pay.
    """
    task = make_task(status=TaskStatus.COMPLETED, confirmed_by=1)  # the requester
    req = make_request(status=LeaveStatus.APPROVED, task=task)
    assert svc.credited_days(req) == {MON: False}


def test_self_confirmation_can_be_allowed_by_policy():
    svc = LeaveService(WorkCalendar(), DelegationPolicy(allow_self_confirmation=True))
    task = make_task(status=TaskStatus.COMPLETED, confirmed_by=1)
    req = make_request(status=LeaveStatus.APPROVED, task=task)
    assert svc.credited_days(req) == {MON: True}


def test_unapproved_leave_is_never_credited(svc):
    for st in (LeaveStatus.PENDING, LeaveStatus.REJECTED, LeaveStatus.CANCELLED):
        req = make_request(
            status=st, task=make_task(status=TaskStatus.COMPLETED, confirmed_by=3)
        )
        assert svc.credited_days(req) == {}


def test_annual_credit_cap_stops_unlimited_paid_leave(svc):
    """
    Without a cap, an employee could convert their whole year into paid
    absence by delegating every day. The cap is what makes the rule safe.
    """
    task = make_task(
        covers_from=MON, covers_to=date(2026, 10, 16),
        status=TaskStatus.COMPLETED, confirmed_by=3,
    )
    req = make_request(
        start_date=MON, end_date=date(2026, 10, 16),
        status=LeaveStatus.APPROVED, task=task,
    )
    credited = svc.credited_days(req, credited_so_far_this_year=0.0)
    assert sum(1 for v in credited.values() if v) == 10  # the policy cap
    assert sum(1 for v in credited.values() if not v) > 0


def test_credit_already_used_this_year_reduces_the_budget(svc):
    task = make_task(
        covers_from=MON, covers_to=FRI, status=TaskStatus.COMPLETED, confirmed_by=3
    )
    req = make_request(
        start_date=MON, end_date=FRI, status=LeaveStatus.APPROVED, task=task
    )
    credited = svc.credited_days(req, credited_so_far_this_year=8.0)
    assert sum(1 for v in credited.values() if v) == 2


def test_days_outside_the_task_coverage_are_not_credited(svc):
    """Delegating one day must not pay for the whole week."""
    task = make_task(covers_from=MON, covers_to=MON,
                     status=TaskStatus.COMPLETED, confirmed_by=3)
    req = make_request(
        start_date=MON, end_date=FRI, status=LeaveStatus.APPROVED, task=task
    )
    credited = svc.credited_days(req)
    assert credited[MON] is True
    assert all(v is False for d, v in credited.items() if d != MON)


def test_task_not_spanning_the_leave_is_refused_at_validation(svc, balance):
    task = make_task(covers_from=MON, covers_to=MON)
    req = make_request(start_date=MON, end_date=FRI, task=task)
    with pytest.raises(LeaveError):
        svc.validate(req, balance, [])


def test_overloaded_delegate_is_refused(svc, balance):
    """Stops the same reliable colleague absorbing everyone else's work."""
    req = make_request(task=make_task())
    with pytest.raises(LeaveError) as e:
        svc.validate(req, balance, [], delegate_open_tasks=3)
    assert "ሌላ ሰራተኛ" in e.value.message_am


def test_delegate_just_under_the_limit_is_accepted(svc, balance):
    svc.validate(make_request(task=make_task()), balance, [], delegate_open_tasks=2)


def test_unpaid_leave_is_never_credited_even_if_delegated(svc):
    req = make_request(
        leave_type=LeaveType.UNPAID, status=LeaveStatus.APPROVED,
        task=make_task(status=TaskStatus.COMPLETED, confirmed_by=3),
    )
    assert req.is_credited is False


# -- end to end with the hours engine ------------------------------------


def test_credited_leave_pays_the_expected_hours(svc):
    task = make_task(covers_from=MON, covers_to=FRI,
                     status=TaskStatus.COMPLETED, confirmed_by=3)
    req = make_request(
        start_date=MON, end_date=FRI, status=LeaveStatus.APPROVED, task=task
    )
    leave_map = svc.leave_days_for_period([req], MON, FRI)
    engine = HoursEngine(svc.calendar)
    p = engine.compute_period(1, MON, FRI, [], leave_days=leave_map)
    assert p.expected_hours == 37.5
    assert p.worked_hours == 0.0
    assert p.credited_hours == 37.5
    assert p.completion_ratio == 1.0
    assert all(d.status is DayStatus.LEAVE_COVERED for d in p.days)


def test_uncovered_leave_is_also_paid_but_marked_differently(svc):
    req = make_request(start_date=MON, end_date=FRI, status=LeaveStatus.APPROVED)
    leave_map = svc.leave_days_for_period([req], MON, FRI)
    engine = HoursEngine(svc.calendar)
    p = engine.compute_period(1, MON, FRI, [], leave_days=leave_map)
    assert p.credited_hours == 37.5
    assert all(d.status is DayStatus.ON_LEAVE for d in p.days)


def test_absence_without_leave_is_not_paid(svc):
    engine = HoursEngine(svc.calendar)
    p = engine.compute_period(1, MON, FRI, [], leave_days={})
    assert p.credited_hours == 0.0
    assert p.completion_ratio == 0.0
    assert p.count(DayStatus.ABSENT) == 5
