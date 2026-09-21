"""
የፈቃድ አስተዳደር — leave management, with task delegation.

Leave is what stops the attendance reports being wrong. Without it, anyone
on approved leave or official duty shows as an unexplained absence and the
monthly report loses credibility within two weeks.

The delegation rule, as specified: when an employee requests leave they may
optionally hand their work to a colleague. If they do, and the colleague
completes it, the leave day is CREDITED — paid as though worked — rather
than merely excused.

That rule is a policy decision and it is implemented here faithfully, but it
has sharp edges that the code guards against. See DelegationPolicy below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

from .ethiopian import EthiopianDate, iter_days
from .workcalendar import WorkCalendar


class LeaveType(str, Enum):
    ANNUAL = "annual"           # ዓመታዊ ፈቃድ
    SICK = "sick"               # የህመም ፈቃድ
    MATERNITY = "maternity"     # የወሊድ ፈቃድ
    PATERNITY = "paternity"     # የአባትነት ፈቃድ
    UNPAID = "unpaid"           # ያለክፍያ ፈቃድ
    OFFICIAL_DUTY = "official"  # በስራ ጉዳይ
    SHORT = "short"             # የአጭር ጊዜ ፈቃድ (hours)

    @property
    def label_am(self) -> str:
        return {
            LeaveType.ANNUAL: "ዓመታዊ ፈቃድ",
            LeaveType.SICK: "የህመም ፈቃድ",
            LeaveType.MATERNITY: "የወሊድ ፈቃድ",
            LeaveType.PATERNITY: "የአባትነት ፈቃድ",
            LeaveType.UNPAID: "ያለክፍያ ፈቃድ",
            LeaveType.OFFICIAL_DUTY: "በስራ ጉዳይ",
            LeaveType.SHORT: "የአጭር ጊዜ ፈቃድ",
        }[self]

    @property
    def is_paid(self) -> bool:
        return self is not LeaveType.UNPAID

    @property
    def consumes_balance(self) -> bool:
        """Official duty is work, not leave — it never costs anyone a day."""
        return self not in (LeaveType.OFFICIAL_DUTY,)


class LeaveStatus(str, Enum):
    PENDING = "pending"     # በመጠባበቅ ላይ
    APPROVED = "approved"   # ጸድቋል
    REJECTED = "rejected"   # ተቀባይነት አላገኘም
    CANCELLED = "cancelled"  # ተሰርዟል

    @property
    def label_am(self) -> str:
        return {
            LeaveStatus.PENDING: "በመጠባበቅ ላይ",
            LeaveStatus.APPROVED: "ጸድቋል",
            LeaveStatus.REJECTED: "ተቀባይነት አላገኘም",
            LeaveStatus.CANCELLED: "ተሰርዟል",
        }[self]


class TaskStatus(str, Enum):
    ASSIGNED = "assigned"    # ተሰጥቷል
    IN_PROGRESS = "progress"  # በስራ ላይ
    COMPLETED = "completed"  # ተጠናቋል
    NOT_DONE = "not_done"    # አልተሰራም

    @property
    def label_am(self) -> str:
        return {
            TaskStatus.ASSIGNED: "ተሰጥቷል",
            TaskStatus.IN_PROGRESS: "በስራ ላይ",
            TaskStatus.COMPLETED: "ተጠናቋል",
            TaskStatus.NOT_DONE: "አልተሰራም",
        }[self]


class LeaveError(ValueError):
    """A leave request that cannot be accepted, with an Amharic reason."""

    def __init__(self, message_am: str, detail: str = "") -> None:
        self.message_am = message_am
        self.detail = detail
        super().__init__(detail or message_am)


@dataclass
class DelegationPolicy:
    """
    Guards on the credit-for-delegated-leave rule.

    The rule as stated — delegate your task, keep your pay — is reasonable
    for occasional cover. Left ungoverned it has three failure modes, each
    of which this policy closes:

      1. It converts unlimited leave into paid presence. `max_credited_days
         _per_year` caps how much of a year can be paid this way.

      2. The colleague absorbs the extra work with nothing to show for it.
         `max_concurrent_delegations` stops the same few reliable people
         being loaded up, and every delegation is recorded against the
         delegate so the distribution is visible.

      3. Credit for work nobody verified. `require_completion_confirmation`
         means the day is credited only once the task is confirmed done,
         and `confirm_by` records who confirmed it.

    All four are settings, not constants — HR will tune them as the rules
    are written, and they must be adjustable without a code change.
    """

    max_credited_days_per_year: int = 10
    max_concurrent_delegations: int = 3
    require_completion_confirmation: bool = True
    allow_self_confirmation: bool = False


@dataclass
class DelegatedTask:
    """Work handed to a colleague so a leave day can be credited."""

    id: int | None
    leave_request_id: int
    description: str
    delegated_to: int
    delegated_by: int
    covers_from: date
    covers_to: date
    status: TaskStatus = TaskStatus.ASSIGNED
    confirmed_by: int | None = None
    confirmed_at: datetime | None = None
    completion_note: str = ""

    def __post_init__(self) -> None:
        if self.delegated_to == self.delegated_by:
            raise LeaveError(
                "ተግባሩን ለራስዎ መስጠት አይችሉም",
                "an employee cannot delegate a task to themselves",
            )
        if self.covers_to < self.covers_from:
            raise LeaveError("የተግባሩ ጊዜ ትክክል አይደለም", "covers_to precedes covers_from")

    @property
    def is_complete(self) -> bool:
        return self.status is TaskStatus.COMPLETED

    def covers(self, day: date) -> bool:
        return self.covers_from <= day <= self.covers_to


@dataclass
class LeaveRequest:
    id: int | None
    employee_id: int
    leave_type: LeaveType
    start_date: date
    end_date: date
    reason: str = ""
    status: LeaveStatus = LeaveStatus.PENDING
    requested_at: datetime | None = None
    decided_by: int | None = None
    decided_at: datetime | None = None
    decision_note: str = ""
    task: DelegatedTask | None = None
    # for LeaveType.SHORT, hours rather than whole days
    hours: float | None = None

    def __post_init__(self) -> None:
        if self.end_date < self.start_date:
            raise LeaveError(
                "የመጨረሻ ቀን ከመጀመሪያ ቀን ሊቀድም አይችልም",
                "end_date precedes start_date",
            )
        if self.leave_type is LeaveType.SHORT and self.hours is None:
            raise LeaveError(
                "የአጭር ጊዜ ፈቃድ የሰዓት መጠን ይፈልጋል",
                "short leave requires hours",
            )

    def dates(self) -> list[date]:
        return list(iter_days(self.start_date, self.end_date))

    def working_days(self, calendar: WorkCalendar) -> list[date]:
        """
        Days actually consumed from the balance.

        Leave that spans a weekend or a public holiday must not eat the
        balance for those days — nobody was due to work them.
        """
        return [d for d in self.dates() if calendar.day_type(d).is_working]

    def balance_cost(self, calendar: WorkCalendar) -> float:
        if not self.leave_type.consumes_balance:
            return 0.0
        if self.leave_type is LeaveType.SHORT:
            return 0.0
        return float(len(self.working_days(calendar)))

    @property
    def is_credited(self) -> bool:
        """Whether these days are paid as though worked."""
        if self.status is not LeaveStatus.APPROVED:
            return False
        if self.leave_type is LeaveType.UNPAID:
            return False
        return True

    @property
    def is_task_covered(self) -> bool:
        return self.task is not None and self.task.is_complete


@dataclass
class LeaveBalance:
    """An employee's entitlement and usage for one Ethiopian year."""

    employee_id: int
    eth_year: int
    entitlements: dict[LeaveType, float] = field(default_factory=dict)
    used: dict[LeaveType, float] = field(default_factory=dict)

    def entitlement(self, lt: LeaveType) -> float:
        return self.entitlements.get(lt, 0.0)

    def taken(self, lt: LeaveType) -> float:
        return self.used.get(lt, 0.0)

    def remaining(self, lt: LeaveType) -> float:
        if not lt.consumes_balance:
            return float("inf")
        return round(self.entitlement(lt) - self.taken(lt), 2)

    def summary_am(self) -> list[dict]:
        rows = []
        for lt in LeaveType:
            if not lt.consumes_balance:
                continue
            ent = self.entitlement(lt)
            if ent <= 0:
                continue
            rows.append(
                {
                    "የፈቃድ ዓይነት": lt.label_am,
                    "የተፈቀደ": ent,
                    "የተጠቀመ": self.taken(lt),
                    "ቀሪ": self.remaining(lt),
                }
            )
        return rows


DEFAULT_ENTITLEMENTS: dict[LeaveType, float] = {
    LeaveType.ANNUAL: 10.0,
    LeaveType.SICK: 10.0,
    LeaveType.MATERNITY: 120.0,
    LeaveType.PATERNITY: 3.0,
    LeaveType.UNPAID: 0.0,
}


class LeaveService:
    """Validates and applies leave requests against balances and the calendar."""

    def __init__(
        self,
        calendar: WorkCalendar | None = None,
        policy: DelegationPolicy | None = None,
    ) -> None:
        self.calendar = calendar or WorkCalendar()
        self.policy = policy or DelegationPolicy()

    # -- validation ----------------------------------------------------

    def validate(
        self,
        request: LeaveRequest,
        balance: LeaveBalance,
        existing: list[LeaveRequest],
        *,
        delegate_open_tasks: int = 0,
        today: date | None = None,
    ) -> None:
        """Raise LeaveError with an Amharic message, or return cleanly."""
        today = today or date.today()

        working = request.working_days(self.calendar)
        if not working and request.leave_type is not LeaveType.SHORT:
            raise LeaveError(
                "የተመረጡት ቀናት የስራ ቀናት አይደሉም",
                "requested range contains no working days",
            )

        # Overlap with an existing live request for the same employee.
        for other in existing:
            if other.id is not None and other.id == request.id:
                continue
            if other.employee_id != request.employee_id:
                continue
            if other.status not in (LeaveStatus.PENDING, LeaveStatus.APPROVED):
                continue
            if (
                request.start_date <= other.end_date
                and other.start_date <= request.end_date
            ):
                raise LeaveError(
                    "በእነዚህ ቀናት ሌላ የፈቃድ ጥያቄ አለ",
                    f"overlaps leave request {other.id}",
                )

        # Balance.
        cost = request.balance_cost(self.calendar)
        if cost > 0:
            remaining = balance.remaining(request.leave_type)
            if cost > remaining:
                raise LeaveError(
                    f"የቀረዎት {request.leave_type.label_am} "
                    f"{remaining:g} ቀን ብቻ ነው። {cost:g} ቀን ጠይቀዋል።",
                    f"insufficient balance: need {cost}, have {remaining}",
                )

        # Delegation guards.
        task = request.task
        if task is not None:
            if delegate_open_tasks >= self.policy.max_concurrent_delegations:
                raise LeaveError(
                    "የተመረጠው ሰራተኛ በአሁኑ ጊዜ በቂ ተግባራት ተሰጥቶታል። "
                    "ሌላ ሰራተኛ ይምረጡ።",
                    "delegate is at the concurrent-task limit",
                )
            if not task.covers(request.start_date) or not task.covers(
                request.end_date
            ):
                raise LeaveError(
                    "የተግባሩ ጊዜ ከፈቃዱ ጊዜ ጋር አይዛመድም",
                    "task coverage does not span the leave period",
                )

    # -- the credit decision -------------------------------------------

    def credited_days(
        self,
        request: LeaveRequest,
        credited_so_far_this_year: float = 0.0,
    ) -> dict[date, bool]:
        """
        Map each working day of an approved leave to whether it is CREDITED
        (paid as worked) because the task was delegated and completed.

        Returns {} for a request that is not approved. A day maps to False
        when it is ordinary approved leave, and True when the delegation
        rule applies to it.
        """
        if request.status is not LeaveStatus.APPROVED:
            return {}

        days = request.working_days(self.calendar)
        if request.task is None:
            return {d: False for d in days}

        task = request.task

        if self.policy.require_completion_confirmation and not task.is_complete:
            # Delegated but not yet confirmed done — no credit yet. The day
            # becomes credited retroactively when the task is confirmed,
            # which is why nothing here is ever stored.
            return {d: False for d in days}

        if (
            self.policy.require_completion_confirmation
            and not self.policy.allow_self_confirmation
            and task.confirmed_by is not None
            and task.confirmed_by == request.employee_id
        ):
            # The person on leave confirmed their own cover. Not acceptable.
            return {d: False for d in days}

        result: dict[date, bool] = {}
        budget = self.policy.max_credited_days_per_year - credited_so_far_this_year
        for d in days:
            if task.covers(d) and budget > 0:
                result[d] = True
                budget -= 1
            else:
                result[d] = False
        return result

    def apply_to_balance(
        self, request: LeaveRequest, balance: LeaveBalance
    ) -> LeaveBalance:
        """Consume the balance for an approved request."""
        if request.status is not LeaveStatus.APPROVED:
            return balance
        cost = request.balance_cost(self.calendar)
        if cost <= 0:
            return balance
        balance.used[request.leave_type] = (
            balance.used.get(request.leave_type, 0.0) + cost
        )
        return balance

    def release_balance(
        self, request: LeaveRequest, balance: LeaveBalance
    ) -> LeaveBalance:
        """Give the days back when approved leave is cancelled."""
        cost = request.balance_cost(self.calendar)
        if cost <= 0:
            return balance
        balance.used[request.leave_type] = max(
            0.0, balance.used.get(request.leave_type, 0.0) - cost
        )
        return balance

    # -- helpers --------------------------------------------------------

    def leave_days_for_period(
        self,
        requests: list[LeaveRequest],
        start: date,
        end: date,
        credited_so_far: float = 0.0,
    ) -> dict[date, bool]:
        """
        Build the {date: covered} map the HoursEngine consumes, for all of
        an employee's approved leave overlapping a period.
        """
        out: dict[date, bool] = {}
        running = credited_so_far
        for req in sorted(requests, key=lambda r: r.start_date):
            if req.status is not LeaveStatus.APPROVED:
                continue
            credited = self.credited_days(req, running)
            for d, is_credited in credited.items():
                if start <= d <= end:
                    out[d] = is_credited
                if is_credited:
                    running += 1
        return out

    @staticmethod
    def balance_year_for(day: date, reset_month: int = 11) -> int:
        """
        The Ethiopian year a leave balance belongs to.

        reset_month 11 is ሐምሌ, the start of the Ethiopian fiscal year;
        pass 1 for መስከረም if HR resets on the calendar year instead.
        """
        eth = EthiopianDate.from_gregorian(day)
        if eth.month >= reset_month:
            return eth.year
        return eth.year - 1
