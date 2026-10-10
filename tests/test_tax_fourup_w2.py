"""The IRS four-up sheet, which is what an employer actually hands out.

Every test here corresponds to something that was broken when a real sheet
was first put through the reader, and every one of those faults existed
because the only forms the reader had ever seen were forms this project
generated itself. The generator and the reader shared all their
assumptions, so the tests passed and the product did not work.

The faults, in the order they appeared:

  * the employee's name came back as "Ann M. Smith Ann M. Smith", because
    the name window was wider than a copy and reached into the next one;
  * the employer's name was not found at all, because that same window
    swallowed the money boxes printed beside it and the result was
    rejected for containing digits;
  * the state came back as **ID**, Idaho, scraped out of the printed words
    "Employer ID no." -- a Kentucky return quietly priced on Idaho's rates;
  * boxes 1, 3 and 5 read as empty off a photograph, because OCR split
    "37847.06" into "37847" and ".06" and neither half is a number;
  * boxes 3 and 5 then read their neighbours' figures, because the search
    window was wider than the narrow columns on this sheet.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fixtures_fourup_w2 import (  # noqa: E402
    EIN,
    EMPLOYER,
    FED,
    MED_TAX,
    SS_TAX,
    ST_TAX,
    WAGES,
    pdf_bytes,
    photo_bytes,
)
from taxvault.forms import ocr  # noqa: E402
from taxvault.forms.w2_layout import read_w2_image, read_w2_layout  # noqa: E402

pytest_plugins = ["tests.conftest_taxvault"]

needs_ocr = pytest.mark.skipif(not ocr.available()["pdfs"],
                               reason="Tesseract or poppler is not installed")

WAGE = float(WAGES)


@pytest.fixture(scope="module")
def sheet_pdf() -> bytes:
    return pdf_bytes(4)


@pytest.fixture(scope="module")
def sheet_photo() -> bytes:
    return photo_bytes(4)


def _check(form, *, where: str):
    assert float(form.wages) == pytest.approx(WAGE, abs=1), where
    assert float(form.federal_withheld) == pytest.approx(float(FED), abs=1), where
    assert float(form.social_security_wages) == pytest.approx(WAGE, abs=1), where
    assert float(form.social_security_withheld) == pytest.approx(float(SS_TAX), abs=1), where
    assert float(form.medicare_wages) == pytest.approx(WAGE, abs=1), where
    assert float(form.medicare_withheld) == pytest.approx(float(MED_TAX), abs=1), where


# ------------------------------------------------------------ the text PDF
def test_a_four_up_pdf_reads_every_federal_box(sheet_pdf):
    form, confidence, _ = read_w2_layout(sheet_pdf)
    assert confidence == 1.0
    _check(form, where="four-up PDF")


def test_the_employee_name_is_not_repeated_once_per_copy(sheet_pdf):
    form, _, _ = read_w2_layout(sheet_pdf)
    whole = f"{form.employee_first_name} {form.employee_last_name}".strip()
    assert whole == "Ann M. Smith", whole
    assert whole.count("Smith") == 1


def test_the_employer_is_read_and_not_lost_to_the_box_beside_it(sheet_pdf):
    """The name block is read with a window. Too wide and it takes in the
    money boxes printed alongside, and the name is thrown away for having
    digits in it."""
    form, _, _ = read_w2_layout(sheet_pdf)
    assert form.employer_name == EMPLOYER[0]
    assert not any(ch.isdigit() for ch in form.employer_name)


def test_the_state_is_kentucky_and_not_idaho(sheet_pdf):
    """The old fallback swept the page for any two-letter state abbreviation
    and took the first, which on a real sheet is the "ID" in "Employer ID
    no." Every form it could not read properly came back as Idaho."""
    form, _, _ = read_w2_layout(sheet_pdf)
    assert [line.state for line in form.states] == ["KY"]
    assert float(form.states[0].state_withheld) == pytest.approx(float(ST_TAX), abs=1)
    assert float(form.states[0].state_wages) == pytest.approx(WAGE, abs=1)


def test_the_sheet_says_it_carries_several_copies(sheet_pdf):
    _, _, notes = read_w2_layout(sheet_pdf)
    assert any("copies of the same W-2" in n["message"] for n in notes), notes


def test_a_sample_form_with_no_real_year_says_so(sheet_pdf):
    """A blank or sample W-2 prints "20XX". Taking that as a year is
    impossible and ignoring it silently is worse: the rates change every
    year, so the one that gets used has to be stated."""
    form, _, notes = read_w2_layout(sheet_pdf)
    assert not form.tax_year
    assert any("20XX" in n["message"] for n in notes), notes


# -------------------------------------------------------- the photographed
@needs_ocr
def test_a_photographed_four_up_sheet_reads_every_federal_box(sheet_photo):
    form, _, _ = read_w2_image(sheet_photo, content_type="image/jpeg")
    _check(form, where="four-up photo")


@needs_ocr
def test_the_state_comes_off_the_photograph_too(sheet_photo):
    """OCR runs the code and the employer's state ID together --
    "KY_A123456789" -- so splitting on whitespace finds no state at all and
    the return is computed federal-only, silently."""
    form, _, _ = read_w2_image(sheet_photo, content_type="image/jpeg")
    assert [line.state for line in form.states] == ["KY"]
    assert float(form.states[0].state_withheld) == pytest.approx(float(ST_TAX), abs=2)


@needs_ocr
def test_the_names_survive_the_photograph(sheet_photo):
    form, _, _ = read_w2_image(sheet_photo, content_type="image/jpeg")
    assert form.employer_name == EMPLOYER[0]
    assert form.employee_last_name == "Smith"


@needs_ocr
def test_a_single_copy_photograph_reads_as_well_as_the_sheet():
    """The copies are not the only difficulty: this form's print is smaller
    than the one the reader was built against, which is what broke the row
    grouping."""
    form, _, _ = read_w2_image(photo_bytes(1), content_type="image/jpeg")
    _check(form, where="single-copy photo")


# ------------------------------------------------------- the split decimal
def test_an_amount_split_at_the_decimal_point_is_put_back_together():
    """Tesseract splits a small amount at a faint decimal point. Neither
    half is a number, so the box reads as empty -- an estimate computed
    without the client's wages in it."""
    from taxvault.forms.ocr import _rejoin_amounts

    joined = _rejoin_amounts([
        {"text": "37847", "left": 442, "top": 116, "width": 58, "height": 14},
        {"text": ".06", "left": 506, "top": 116, "width": 30, "height": 14},
    ])
    assert [t["text"] for t in joined] == ["37847.06"]


def test_two_separate_figures_are_not_run_together():
    """The repair has to be geometric. Two amounts in neighbouring boxes are
    far apart; the halves of one amount are touching."""
    from taxvault.forms.ocr import _rejoin_amounts

    apart = _rejoin_amounts([
        {"text": "37847", "left": 442, "top": 116, "width": 58, "height": 14},
        {"text": ".06", "left": 700, "top": 116, "width": 30, "height": 14},
    ])
    assert [t["text"] for t in apart] == ["37847", ".06"]

    different_rows = _rejoin_amounts([
        {"text": "37847", "left": 442, "top": 116, "width": 58, "height": 14},
        {"text": ".06", "left": 506, "top": 160, "width": 30, "height": 14},
    ])
    assert [t["text"] for t in different_rows] == ["37847", ".06"]


# ------------------------------------------------------------- end to end
@needs_ocr
def test_the_whole_thing_from_a_photograph_to_a_kentucky_estimate(tmp_path, capsys,
                                                                  sheet_photo):
    from taxvault.cli import main

    path = tmp_path / "w2-4up.jpg"
    path.write_bytes(sheet_photo)
    assert main(["estimate", "--w2", str(path), "--status", "single",
                 "--year", "2025"]) == 0
    out = capsys.readouterr().out
    assert "37,847.06" in out
    assert "KY" in out and "Kentucky" in out
    assert "Idaho" not in out
