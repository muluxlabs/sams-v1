"""
የኢትዮጵያ የቀን አቆጣጠር — Ethiopian calendar.

Conversion between the Ethiopian and Gregorian calendars via Julian Day Number,
plus Amharic month and weekday names and Ethiopian-month period boundaries.

The Ethiopian year has 12 months of 30 days plus ጳጉሜ (Pagume) of 5 days,
or 6 days in a leap year (year % 4 == 3).

Storage rule for the whole system: dates are stored as Gregorian ISO strings.
Ethiopian dates exist only at the presentation layer. Never store an Ethiopian
date in the database — the conversion is cheap and one-directional storage
avoids an entire class of bug.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterator

# JDN of Meskerem 1, year 1 EC (= 29 August 8 CE, Julian calendar).
_ETHIOPIC_EPOCH = 1724221

MONTH_NAMES_AM = (
    "መስከረም", "ጥቅምት", "ኅዳር", "ታኅሣሥ", "ጥር", "የካቲት",
    "መጋቢት", "ሚያዝያ", "ግንቦት", "ሰኔ", "ሐምሌ", "ነሐሴ", "ጳጉሜ",
)

MONTH_NAMES_LATIN = (
    "Meskerem", "Tikimt", "Hidar", "Tahsas", "Tir", "Yekatit",
    "Megabit", "Miyazya", "Ginbot", "Sene", "Hamle", "Nehase", "Pagume",
)

# Monday = 0, matching datetime.date.weekday()
WEEKDAY_NAMES_AM = ("ሰኞ", "ማክሰኞ", "ረቡዕ", "ሐሙስ", "ዓርብ", "ቅዳሜ", "እሁድ")

WEEKDAY_NAMES_LATIN = (
    "Segno", "Maksegno", "Rebu", "Hamus", "Arb", "Kidame", "Ehud",
)


class EthiopianDateError(ValueError):
    """Raised for an Ethiopian date that does not exist."""


def _gregorian_to_jdn(year: int, month: int, day: int) -> int:
    a = (14 - month) // 12
    y = year + 4800 - a
    m = month + 12 * a - 3
    return (
        day
        + (153 * m + 2) // 5
        + 365 * y
        + y // 4
        - y // 100
        + y // 400
        - 32045
    )


def _jdn_to_gregorian(jdn: int) -> tuple[int, int, int]:
    a = jdn + 32044
    b = (4 * a + 3) // 146097
    c = a - (146097 * b) // 4
    d = (4 * c + 3) // 1461
    e = c - (1461 * d) // 4
    m = (5 * e + 2) // 153
    day = e - (153 * m + 2) // 5 + 1
    month = m + 3 - 12 * (m // 10)
    year = 100 * b + d - 4800 + m // 10
    return year, month, day


def is_leap_year(eth_year: int) -> bool:
    """ጳጉሜ has 6 days when the Ethiopian year mod 4 equals 3."""
    return eth_year % 4 == 3


def days_in_month(eth_year: int, eth_month: int) -> int:
    """Days in an Ethiopian month. Only ጳጉሜ (13) varies."""
    if not 1 <= eth_month <= 13:
        raise EthiopianDateError(f"month must be 1..13, got {eth_month}")
    if eth_month == 13:
        return 6 if is_leap_year(eth_year) else 5
    return 30


@dataclass(frozen=True, order=True)
class EthiopianDate:
    """An Ethiopian calendar date. Immutable and comparable."""

    year: int
    month: int
    day: int

    def __post_init__(self) -> None:
        if self.year < 1:
            raise EthiopianDateError(f"year must be >= 1, got {self.year}")
        if not 1 <= self.month <= 13:
            raise EthiopianDateError(f"month must be 1..13, got {self.month}")
        limit = days_in_month(self.year, self.month)
        if not 1 <= self.day <= limit:
            raise EthiopianDateError(
                f"{MONTH_NAMES_LATIN[self.month - 1]} {self.year} "
                f"has {limit} days, got day {self.day}"
            )

    # -- conversion ---------------------------------------------------

    def to_jdn(self) -> int:
        return (
            _ETHIOPIC_EPOCH
            - 1
            + 365 * (self.year - 1)
            + self.year // 4
            + 30 * (self.month - 1)
            + self.day
        )

    def to_gregorian(self) -> date:
        return date(*_jdn_to_gregorian(self.to_jdn()))

    @classmethod
    def from_jdn(cls, jdn: int) -> "EthiopianDate":
        r = jdn - (_ETHIOPIC_EPOCH - 1)
        if r < 1:
            raise EthiopianDateError("date precedes the Ethiopian epoch")
        # 1461 days per 4-year cycle. Within a cycle the third year is the
        # leap one (y % 4 == 3, ጳጉሜ has 6 days), so its span is 366 days and
        # the fourth year starts at offset 1096, not 1095. Getting this
        # boundary wrong shifts every date after ጳጉሜ in a leap year by a day.
        n_4y, day_of_cycle = divmod(r - 1, 1461)
        year = 4 * n_4y
        if day_of_cycle < 365:          # year 4k+1, 365 days
            year += 1
            day_of_year = day_of_cycle
        elif day_of_cycle < 730:        # year 4k+2, 365 days
            year += 2
            day_of_year = day_of_cycle - 365
        elif day_of_cycle < 1096:       # year 4k+3, 366 days — the leap year
            year += 3
            day_of_year = day_of_cycle - 730
        else:                           # year 4k+4, 365 days
            year += 4
            day_of_year = day_of_cycle - 1096
        month, day = divmod(day_of_year, 30)
        return cls(year, month + 1, day + 1)

    @classmethod
    def from_gregorian(cls, g: date) -> "EthiopianDate":
        return cls.from_jdn(_gregorian_to_jdn(g.year, g.month, g.day))

    @classmethod
    def today(cls) -> "EthiopianDate":
        return cls.from_gregorian(date.today())

    # -- presentation -------------------------------------------------

    @property
    def month_name_am(self) -> str:
        return MONTH_NAMES_AM[self.month - 1]

    @property
    def month_name_latin(self) -> str:
        return MONTH_NAMES_LATIN[self.month - 1]

    @property
    def weekday(self) -> int:
        """Monday = 0, matching datetime.date.weekday()."""
        return self.to_gregorian().weekday()

    @property
    def weekday_name_am(self) -> str:
        return WEEKDAY_NAMES_AM[self.weekday]

    def format_am(self, with_weekday: bool = False) -> str:
        """'መስከረም 10 ቀን 2019 ዓ.ም.'"""
        base = f"{self.month_name_am} {self.day} ቀን {self.year} ዓ.ም."
        if with_weekday:
            return f"{self.weekday_name_am}፣ {base}"
        return base

    def format_short_am(self) -> str:
        """'10/01/2019'"""
        return f"{self.day:02d}/{self.month:02d}/{self.year}"

    def format_latin(self) -> str:
        return f"{self.month_name_latin} {self.day}, {self.year} EC"

    def __str__(self) -> str:
        return self.format_am()


# -- period helpers ----------------------------------------------------
# Reports must use Ethiopian month boundaries, not Gregorian ones.
# A "monthly report" that cuts at the Gregorian month splits every
# Ethiopian month in the wrong place and every total is wrong.


def month_start(eth_year: int, eth_month: int) -> date:
    return EthiopianDate(eth_year, eth_month, 1).to_gregorian()


def month_end(eth_year: int, eth_month: int) -> date:
    return EthiopianDate(
        eth_year, eth_month, days_in_month(eth_year, eth_month)
    ).to_gregorian()


def month_range(eth_year: int, eth_month: int) -> tuple[date, date]:
    """Inclusive Gregorian (start, end) of an Ethiopian month."""
    return month_start(eth_year, eth_month), month_end(eth_year, eth_month)


def year_range(eth_year: int) -> tuple[date, date]:
    """Inclusive Gregorian (start, end) of an Ethiopian year, ጳጉሜ included."""
    return (
        month_start(eth_year, 1),
        month_end(eth_year, 13),
    )


def iter_days(start: date, end: date) -> Iterator[date]:
    """Inclusive day iterator."""
    if end < start:
        raise ValueError("end precedes start")
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def week_range(any_day: date) -> tuple[date, date]:
    """Monday-to-Sunday week containing the given day."""
    start = any_day - timedelta(days=any_day.weekday())
    return start, start + timedelta(days=6)


def format_gregorian_am(g: date, with_weekday: bool = False) -> str:
    """Convenience: format a Gregorian date as Amharic Ethiopian text."""
    return EthiopianDate.from_gregorian(g).format_am(with_weekday=with_weekday)
