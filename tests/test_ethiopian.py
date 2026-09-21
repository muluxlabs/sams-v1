"""
Destructive tests for the Ethiopian calendar.

Calendar bugs hide until the one week a year they surface, and by then the
system is in production and someone's pay is wrong. So this suite does not
test a few happy dates — it round-trips every day across a multi-century
span and pins the boundaries that actually break implementations.
"""

from datetime import date, timedelta

import pytest

from sams.core.ethiopian import (
    EthiopianDate,
    EthiopianDateError,
    days_in_month,
    is_leap_year,
    month_range,
    week_range,
    year_range,
)


# -- known anchor dates ------------------------------------------------
# Verified pairs. If the epoch constant is ever touched, these fail first.

KNOWN_PAIRS = [
    (EthiopianDate(2019, 1, 10), date(2026, 9, 20)),   # today
    (EthiopianDate(2019, 1, 1), date(2026, 9, 11)),    # እንቁጣጣሽ 2019
    (EthiopianDate(2017, 1, 1), date(2024, 9, 11)),    # እንቁጣጣሽ 2017
    (EthiopianDate(2020, 1, 1), date(2027, 9, 12)),    # after a leap year
    (EthiopianDate(2015, 4, 29), date(2023, 1, 7)),    # ገና
    (EthiopianDate(2016, 4, 29), date(2024, 1, 8)),    # ገና, year after a leap
    (EthiopianDate(2016, 5, 11), date(2024, 1, 20)),   # ጥምቀት
    # The epoch is 29 August 8 CE in the JULIAN calendar, which is
    # 27 August in the proleptic Gregorian calendar that date() uses.
    (EthiopianDate(1, 1, 1), date(8, 8, 27)),
]


def test_genna_shifts_by_a_day_after_a_leap_year():
    """
    ገና is fixed at ታኅሣሥ 29, so in Gregorian terms it lands on 7 January in
    three years out of four and on 8 January in the year following an
    Ethiopian leap year. Any holiday table that hard-codes 7 January marks
    the whole office absent on the real holiday once every four years.
    This is why holidays are defined in Ethiopian terms and stay editable.
    """
    assert EthiopianDate(2015, 4, 29).to_gregorian() == date(2023, 1, 7)
    assert EthiopianDate(2016, 4, 29).to_gregorian() == date(2024, 1, 8)
    assert EthiopianDate(2017, 4, 29).to_gregorian() == date(2025, 1, 7)
    assert EthiopianDate(2018, 4, 29).to_gregorian() == date(2026, 1, 7)


@pytest.mark.parametrize("eth,greg", KNOWN_PAIRS)
def test_known_pairs_convert_both_ways(eth, greg):
    assert eth.to_gregorian() == greg
    assert EthiopianDate.from_gregorian(greg) == eth


def test_epoch_is_day_one():
    assert EthiopianDate(1, 1, 1).to_jdn() == 1724221


# -- exhaustive round trip ---------------------------------------------


def test_round_trip_every_day_for_two_centuries():
    """
    Every single day from 1900 to 2100 Gregorian, converted to Ethiopian
    and back. 73,000-odd days. If any off-by-one exists anywhere in the
    cycle arithmetic, this finds it.
    """
    cur = date(1900, 1, 1)
    stop = date(2100, 12, 31)
    count = 0
    while cur <= stop:
        eth = EthiopianDate.from_gregorian(cur)
        assert eth.to_gregorian() == cur, f"round trip failed on {cur}"
        cur += timedelta(days=1)
        count += 1
    assert count > 73_000


def test_consecutive_days_are_consecutive():
    """Ethiopian dates must advance by exactly one day, including across
    month and year boundaries — especially ነሐሴ 30 -> ጳጉሜ 1 -> መስከረም 1."""
    cur = date(2020, 1, 1)
    stop = date(2040, 12, 31)
    prev = EthiopianDate.from_gregorian(cur)
    while cur < stop:
        cur += timedelta(days=1)
        nxt = EthiopianDate.from_gregorian(cur)
        assert nxt.to_jdn() - prev.to_jdn() == 1, f"gap at {cur}"
        prev = nxt


# -- ጳጉሜ and leap years -------------------------------------------------


@pytest.mark.parametrize(
    "year,expected",
    [(2011, True), (2015, True), (2019, True), (2012, False), (2018, False)],
)
def test_leap_year_rule(year, expected):
    assert is_leap_year(year) is expected
    assert days_in_month(year, 13) == (6 if expected else 5)


def test_pagume_6_exists_only_in_leap_years():
    EthiopianDate(2019, 13, 6)  # leap — fine
    with pytest.raises(EthiopianDateError):
        EthiopianDate(2018, 13, 6)


def test_pagume_7_never_exists():
    for y in (2018, 2019, 2020, 2021):
        with pytest.raises(EthiopianDateError):
            EthiopianDate(y, 13, 7)


def test_pagume_6_converts_and_returns():
    d = EthiopianDate(2019, 13, 6)
    assert EthiopianDate.from_gregorian(d.to_gregorian()) == d


def test_day_after_pagume_is_new_year():
    """The boundary that breaks naive implementations."""
    for y in (2018, 2019, 2020, 2021, 2022):
        last = EthiopianDate(y, 13, days_in_month(y, 13))
        nxt = EthiopianDate.from_gregorian(last.to_gregorian() + timedelta(days=1))
        assert nxt == EthiopianDate(y + 1, 1, 1)


def test_every_year_has_365_or_366_days():
    for y in range(1990, 2060):
        start, end = year_range(y)
        length = (end - start).days + 1
        assert length == (366 if is_leap_year(y) else 365), f"year {y} = {length}"


# -- invalid input ------------------------------------------------------


@pytest.mark.parametrize(
    "y,m,d",
    [
        (2019, 0, 1), (2019, 14, 1), (2019, 1, 0),
        (2019, 1, 31), (2019, 12, 31), (0, 1, 1), (-5, 1, 1),
    ],
)
def test_invalid_dates_are_refused(y, m, d):
    with pytest.raises(EthiopianDateError):
        EthiopianDate(y, m, d)


def test_dates_before_epoch_are_refused():
    with pytest.raises(EthiopianDateError):
        EthiopianDate.from_jdn(1724220)


# -- month boundaries used by reports -----------------------------------


def test_month_range_covers_exactly_the_month():
    for y in (2018, 2019):
        for m in range(1, 14):
            start, end = month_range(y, m)
            assert (end - start).days + 1 == days_in_month(y, m)
            assert EthiopianDate.from_gregorian(start) == EthiopianDate(y, m, 1)


def test_months_tile_the_year_without_gap_or_overlap():
    """Monthly reports must partition the year exactly — no day counted
    twice, no day missed. This is what makes annual totals reconcile."""
    for y in (2018, 2019, 2020):
        days = []
        for m in range(1, 14):
            start, end = month_range(y, m)
            cur = start
            while cur <= end:
                days.append(cur)
                cur += timedelta(days=1)
        assert len(days) == len(set(days)), "a day appeared in two months"
        y_start, y_end = year_range(y)
        assert min(days) == y_start and max(days) == y_end
        assert len(days) == (366 if is_leap_year(y) else 365)


def test_ethiopian_month_does_not_align_with_gregorian():
    """The reason reports must not cut on Gregorian months: they disagree."""
    start, end = month_range(2019, 1)
    assert start.month != end.month


# -- ordering and formatting --------------------------------------------


def test_dates_order_correctly():
    assert EthiopianDate(2019, 1, 1) < EthiopianDate(2019, 1, 2)
    assert EthiopianDate(2019, 13, 5) < EthiopianDate(2020, 1, 1)
    assert EthiopianDate(2018, 13, 5) < EthiopianDate(2019, 1, 1)


def test_amharic_formatting():
    d = EthiopianDate(2019, 1, 10)
    assert d.format_am() == "መስከረም 10 ቀን 2019 ዓ.ም."
    assert "እሁድ" in d.format_am(with_weekday=True)
    assert d.format_short_am() == "10/01/2019"


def test_weekday_matches_gregorian():
    for offset in range(0, 500, 7):
        g = date(2026, 1, 1) + timedelta(days=offset)
        assert EthiopianDate.from_gregorian(g).weekday == g.weekday()


def test_week_range_is_monday_to_sunday():
    start, end = week_range(date(2026, 9, 20))  # a Sunday
    assert start.weekday() == 0 and end.weekday() == 6
    assert (end - start).days == 6
    assert start <= date(2026, 9, 20) <= end
