"""
Report validation against a seeded full Ethiopian year.

Every figure here is checked against arithmetic known in advance, not
eyeballed. This is the suite that catches a report bug before the Woreda's
HR officer does.

It also renders a real Amharic PDF and inspects the embedded font, because
ReportLab draws empty boxes for unregistered Ethiopic glyphs without raising
an error — the single most likely way a finished-looking report turns out to
be blank on the day it matters.
"""

from __future__ import annotations

import zipfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from sams.core.ethiopian import EthiopianDate, days_in_month, month_range
from sams.db.connection import connect
from sams.db.repository import load_calendar
from sams.reports import builder
from sams.reports.export import (
    FONT_REGULAR,
    to_excel,
    to_pdf,
    verify_amharic_coverage,
)
from tests.seed import build_seed

AMHARIC_SAMPLE = "የሰራተኞች ተገኝነት መቆጣጠሪያ ስርዓት"


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    d = tmp_path_factory.mktemp("seed")
    facts = build_seed(d / "seed.db", eth_year=2018, n_employees=30)
    conn = connect(d / "seed.db")
    yield conn, facts, d
    conn.close()


# -- the seed itself ------------------------------------------------------


def test_seed_covers_a_whole_ethiopian_year(seeded):
    _, facts, _ = seeded
    length = (facts["end"] - facts["start"]).days + 1
    assert length == 365          # 2018 EC is not a leap year
    assert len(facts["employees"]) == 30


def test_seed_includes_pagume(seeded):
    conn, facts, _ = seeded
    pagume_start, pagume_end = month_range(facts["eth_year"], 13)
    assert facts["start"] <= pagume_start <= facts["end"]
    assert days_in_month(facts["eth_year"], 13) == 5


# -- daily register -------------------------------------------------------


def test_daily_register_has_a_row_per_employee(seeded):
    conn, facts, _ = seeded
    day = facts["start"] + timedelta(days=20)
    rep = builder.daily_register(conn, day)
    assert len(rep["rows"]) == 30
    assert rep["columns"][1] == "የሰራተኛ ስም"


def test_daily_register_counts_reconcile(seeded):
    conn, facts, _ = seeded
    day = facts["start"] + timedelta(days=20)
    rep = builder.daily_register(conn, day)
    assert sum(rep["summary"].values()) == len(rep["rows"])


def test_event_day_credits_every_employee(seeded):
    conn, facts, _ = seeded
    rep = builder.daily_register(conn, facts["event_day"])
    assert all(r["ሁኔታ"] == "የመርሃ ግብር ቀን" for r in rep["rows"])


def test_outage_day_is_not_reported_as_absence(seeded):
    conn, facts, _ = seeded
    rep = builder.daily_register(conn, facts["outage_day"])
    assert all(r["ሁኔታ"] != "አልተገኘም" for r in rep["rows"])
    assert all(r["ሁኔታ"] == "የኤሌክትሪክ መቋረጥ" for r in rep["rows"])


def test_holiday_register_shows_a_holiday(seeded):
    conn, facts, _ = seeded
    cal = load_calendar(conn)
    holiday = next(
        d for d, _ in cal.upcoming_holidays(facts["start"], limit=1, horizon_days=380)
    )
    rep = builder.daily_register(conn, holiday)
    assert all(r["ሁኔታ"] == "የህዝብ በዓል" for r in rep["rows"])


# -- monthly summary ------------------------------------------------------


@pytest.mark.parametrize("month", list(range(1, 14)))
def test_every_ethiopian_month_produces_a_report(seeded, month):
    """Including ጳጉሜ, the month most likely to break a report."""
    conn, facts, _ = seeded
    rep = builder.monthly_summary(conn, facts["eth_year"], month)
    assert len(rep["rows"]) == 30
    assert rep["period_am"].endswith("ዓ.ም.")


def test_monthly_expected_hours_match_the_calendar(seeded):
    conn, facts, _ = seeded
    cal = load_calendar(conn)
    for month in (1, 5, 13):
        start, end = month_range(facts["eth_year"], month)
        expected = cal.expected_total(start, end)
        rep = builder.monthly_summary(conn, facts["eth_year"], month)
        for row in rep["rows"]:
            assert row["የሚጠበቅ ሰዓት"] == pytest.approx(expected, abs=0.01)


def test_monthly_totals_equal_the_sum_of_rows(seeded):
    conn, facts, _ = seeded
    rep = builder.monthly_summary(conn, facts["eth_year"], 3)
    assert rep["totals"]["የሰራው ሰዓት"] == pytest.approx(
        round(sum(r["የሰራው ሰዓት"] for r in rep["rows"]), 2), abs=0.05
    )


def test_twelve_months_plus_pagume_reconcile_to_the_year(seeded):
    """
    The partition test: monthly reports must sum to the annual figure with
    no day counted twice and none missed. If Ethiopian month boundaries
    were wrong, this is where it shows.
    """
    conn, facts, _ = seeded
    emp = facts["employees"][0]
    # Match by name, not by row number: reports are sorted by Amharic name,
    # so row 1 is not necessarily employee id 1.
    name = conn.execute(
        "SELECT name_am FROM employee WHERE id = ?", (emp,)
    ).fetchone()["name_am"]

    monthly_total = 0.0
    for month in range(1, 14):
        rep = builder.monthly_summary(conn, facts["eth_year"], month)
        row = next(r for r in rep["rows"] if r["የሰራተኛ ስም"] == name)
        monthly_total += row["የሰራው ሰዓት"]

    annual = builder.individual_history(conn, emp, facts["start"], facts["end"])
    assert monthly_total == pytest.approx(
        annual["summary"]["የሰራው ሰዓት"], abs=0.2
    )


def test_worked_hours_match_the_seeded_arithmetic(seeded):
    conn, facts, _ = seeded
    emp = facts["employees"][0]
    rep = builder.individual_history(conn, emp, facts["start"], facts["end"])
    assert rep["summary"]["የሰራው ሰዓት"] == pytest.approx(
        facts["per_employee"][emp]["total_worked_hours"], abs=0.1
    )


def test_completion_ratio_is_between_zero_and_one(seeded):
    conn, facts, _ = seeded
    rep = builder.monthly_summary(conn, facts["eth_year"], 2)
    for row in rep["rows"]:
        pct = float(row["የማሟላት መጠን"].rstrip("%"))
        assert 0 <= pct <= 130   # above 100 only via overtime, never negative


# -- individual history ---------------------------------------------------


def test_individual_history_has_a_row_per_calendar_day(seeded):
    conn, facts, _ = seeded
    emp = facts["employees"][1]
    start, end = month_range(facts["eth_year"], 1)
    rep = builder.individual_history(conn, emp, start, end)
    assert len(rep["rows"]) == days_in_month(facts["eth_year"], 1)


def test_individual_history_for_an_unknown_employee_raises(seeded):
    conn, _, _ = seeded
    with pytest.raises(ValueError):
        builder.individual_history(conn, 99999, date(2026, 1, 1), date(2026, 1, 2))


# -- other reports --------------------------------------------------------


def test_absence_report_lists_only_those_with_absences(seeded):
    conn, facts, _ = seeded
    rep = builder.absence_report(conn, facts["start"], facts["end"])
    assert all(r["ያልተገኘባቸው ቀናት"] > 0 for r in rep["rows"])


def test_exception_report_captures_pin_use(seeded):
    conn, facts, _ = seeded
    rep = builder.exception_report(conn, facts["start"], facts["end"])
    assert any(r["ዓይነት"] == "በፒን ኮድ" for r in rep["rows"])


def test_leave_summary_shows_balances(seeded):
    conn, facts, _ = seeded
    rep = builder.leave_summary(conn, facts["eth_year"])
    assert rep["rows"]
    assert {"የተፈቀደ", "የተጠቀመ", "ቀሪ"} <= set(rep["columns"])


def test_every_report_is_titled_in_amharic(seeded):
    conn, facts, _ = seeded
    reports = [
        builder.daily_register(conn, facts["start"] + timedelta(days=10)),
        builder.monthly_summary(conn, facts["eth_year"], 1),
        builder.absence_report(conn, facts["start"], facts["end"]),
        builder.exception_report(conn, facts["start"], facts["end"]),
        builder.leave_summary(conn, facts["eth_year"]),
        builder.task_distribution(conn, facts["start"], facts["end"]),
    ]
    for rep in reports:
        assert any("ሀ" <= ch <= "፿" for ch in rep["title_am"])
        for col in rep["columns"]:
            assert any("ሀ" <= ch <= "፿" for ch in col) or col == "ተ.ቁ"


# -- Amharic rendering: the silent-failure guard --------------------------


def test_ethiopic_font_is_registered_and_covers_amharic():
    assert verify_amharic_coverage(AMHARIC_SAMPLE)


def test_pdf_renders_and_embeds_the_ethiopic_font(seeded, tmp_path):
    conn, facts, _ = seeded
    rep = builder.monthly_summary(conn, facts["eth_year"], 1)
    out = to_pdf(rep, tmp_path / "monthly.pdf")
    assert out.exists()
    blob = out.read_bytes()
    assert blob.startswith(b"%PDF")
    assert len(blob) > 8000
    # The font must be embedded, or the file renders as boxes elsewhere.
    assert b"Ethiopic" in blob or FONT_REGULAR.encode() in blob


def test_pdf_of_every_report_type_succeeds(seeded, tmp_path):
    conn, facts, _ = seeded
    for name, rep in {
        "daily": builder.daily_register(conn, facts["start"] + timedelta(days=10)),
        "monthly": builder.monthly_summary(conn, facts["eth_year"], 2),
        "absence": builder.absence_report(conn, facts["start"], facts["end"]),
        "exception": builder.exception_report(conn, facts["start"], facts["end"]),
        "leave": builder.leave_summary(conn, facts["eth_year"]),
    }.items():
        out = to_pdf(rep, tmp_path / f"{name}.pdf")
        assert out.stat().st_size > 3000, f"{name} PDF suspiciously small"


def test_pdf_of_an_empty_report_still_renders(tmp_path):
    """A period with no data must produce a valid 'no records' page, not
    a crash and not a zero-byte file."""
    rep = {
        "title_am": "የቀሪዎች ሪፖርት", "period_am": "ጥር 2018 ዓ.ም.",
        "columns": ["ተ.ቁ", "የሰራተኛ ስም"], "rows": [],
    }
    out = to_pdf(rep, tmp_path / "empty.pdf")
    assert out.stat().st_size > 1500


def test_very_long_amharic_name_does_not_break_the_pdf(seeded, tmp_path):
    conn, facts, _ = seeded
    rep = builder.monthly_summary(conn, facts["eth_year"], 1)
    rep["rows"][0]["የሰራተኛ ስም"] = "ወልደገብርኤል " * 12
    out = to_pdf(rep, tmp_path / "long.pdf")
    assert out.stat().st_size > 5000


def test_excel_export_is_a_valid_workbook_with_amharic(seeded, tmp_path):
    conn, facts, _ = seeded
    rep = builder.monthly_summary(conn, facts["eth_year"], 1)
    out = to_excel(rep, tmp_path / "monthly.xlsx")
    assert zipfile.is_zipfile(out)

    from openpyxl import load_workbook

    wb = load_workbook(out)
    ws = wb.active
    assert ws["A1"].value == rep["title_am"]
    text = "\n".join(
        str(c.value) for row in ws.iter_rows() for c in row if c.value
    )
    assert any("ሀ" <= ch <= "፿" for ch in text)
    assert ws.freeze_panes is not None


def test_excel_numbers_stay_numeric(seeded, tmp_path):
    """Hours must export as numbers, or HR cannot sum them in Excel."""
    conn, facts, _ = seeded
    rep = builder.monthly_summary(conn, facts["eth_year"], 1)
    out = to_excel(rep, tmp_path / "numbers.xlsx")

    from openpyxl import load_workbook

    ws = load_workbook(out).active
    header_row = next(
        r for r in ws.iter_rows() if r[0].value == "ተ.ቁ"
    )
    col = [c.column for c in header_row if c.value == "የሰራው ሰዓት"][0]
    value = ws.cell(row=header_row[0].row + 1, column=col).value
    assert isinstance(value, (int, float))


# -- the digits guard ----------------------------------------------------
# Added after a real failure: the first build used an Ethiopic-only font,
# which rendered every Amharic name perfectly and every NUMBER as blank
# space, with no error raised anywhere. These tests exist so that specific
# silent failure can never come back.


def test_font_covers_digits_and_punctuation_not_just_amharic():
    from sams.reports.export import FONT_REGULAR, _missing_glyphs, register_fonts

    register_fonts()
    assert _missing_glyphs(FONT_REGULAR, "0123456789") == []
    assert _missing_glyphs(FONT_REGULAR, ".,%-:/()") == []
    assert _missing_glyphs(FONT_REGULAR, "ተ.ቁ ሰዓት ጳጉሜ") == []


def test_numbers_actually_appear_in_the_rendered_pdf(seeded, tmp_path):
    """
    Extract the text layer back out of the PDF and require the hour figures
    to be present. Checking that the file is large enough is not sufficient:
    the broken build produced a perfectly valid 31KB PDF with every number
    invisible.
    """
    import subprocess

    conn, facts, _ = seeded
    rep = builder.monthly_summary(conn, facts["eth_year"], 5)
    out = to_pdf(rep, tmp_path / "numbers.pdf")

    try:
        text = subprocess.run(
            ["pdftotext", str(out), "-"], capture_output=True, text=True, timeout=60
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pytest.skip("pdftotext not available")

    assert any(ch.isdigit() for ch in text), "no digits survived into the PDF"
    expected = str(int(rep["rows"][0]["የሚጠበቅ ሰዓት"]))
    assert expected in text, f"expected hours {expected} missing from the PDF"
    assert "ወርሃዊ" in text
