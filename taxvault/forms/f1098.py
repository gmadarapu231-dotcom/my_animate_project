"""Form 1098, the mortgage interest statement.

Box 1 is the number clients expect to deduct and it is rarely the number they
get to deduct. Three other boxes on the same form decide that:

  * **Box 2**, the outstanding principal, against the $750,000 ceiling.
  * **Box 3 or 11**, the origination date, which decides whether the older
    $1,000,000 ceiling applies instead. A loan from 2016 is worth measurably
    more than the same loan from 2018.
  * **Box 5**, mortgage insurance, which was deductible through 2021, not at
    all for 2022 to 2025, and is deductible again from 2026.

What the form cannot tell you is what a home equity loan paid for, and that is
the test for deducting its interest at all. The lender does not know and does
not report it, so it has to be asked.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from taxvault.money import ZERO, cents, money

_AMOUNT = r"\$?\s*(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+\.\d{2})"
_DATE = r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})"


@dataclass
class F1098:
    lender: str = ""
    tax_year: int = 0
    mortgage_interest: Decimal = ZERO       # box 1
    outstanding_principal: Decimal = ZERO   # box 2
    origination_date: str = ""              # box 3
    refund_of_overpaid: Decimal = ZERO      # box 4
    mortgage_insurance: Decimal = ZERO      # box 5
    points_paid: Decimal = ZERO             # box 6
    property_count: int = 1                 # box 9
    acquisition_date: str = ""              # box 11
    property_address: str = ""

    def best_origination(self) -> str:
        """Box 3 if the lender filled it, otherwise box 11."""
        return self.origination_date or self.acquisition_date

    def to_dict(self) -> dict[str, Any]:
        out = {
            key: (str(cents(value)) if isinstance(value, Decimal) else value)
            for key, value in asdict(self).items()
        }
        out["best_origination"] = self.best_origination()
        return out

    def validate(self, *, tax_year: int = 0) -> list[dict[str, str]]:
        findings: list[dict[str, str]] = []
        if self.mortgage_interest > ZERO and self.outstanding_principal <= ZERO:
            findings.append({
                "severity": "warning", "box": "2",
                "message": (
                    "Box 2 is blank, so the loan balance is unknown and the $750,000 debt "
                    "ceiling cannot be applied. Enter the balance -- above the ceiling the "
                    "interest is prorated, and a blank here quietly assumes it is not."
                ),
            })
        if self.mortgage_interest > ZERO and not self.best_origination():
            findings.append({
                "severity": "warning", "box": "3",
                "message": (
                    "No origination date on the form. A loan taken on or before 15 December "
                    "2017 keeps the older $1,000,000 ceiling for its whole life, which is "
                    "worth real money on a large mortgage -- enter the date from your "
                    "closing documents."
                ),
            })
        if self.mortgage_insurance > ZERO:
            findings.append({
                "severity": "info", "box": "5",
                "message": (
                    f"Box 5 shows {self.mortgage_insurance:,.2f} of mortgage insurance. "
                    "It is not deductible for tax years 2022 to 2025 and becomes "
                    "deductible again from 2026, phasing out above $100,000 of income."
                ),
            })
        if self.points_paid > ZERO:
            findings.append({
                "severity": "info", "box": "6",
                "message": (
                    f"Box 6 shows {self.points_paid:,.2f} of points. On the purchase of "
                    "your main home these come off in full this year; on a refinance they "
                    "are spread over the term of the loan."
                ),
            })
        if self.refund_of_overpaid > ZERO:
            findings.append({
                "severity": "warning", "box": "4",
                "message": (
                    f"Box 4 shows a {self.refund_of_overpaid:,.2f} refund of interest you "
                    "deducted in an earlier year. It generally has to be reported as income "
                    "this year, not netted off box 1."
                ),
            })
        if self.property_count > 1:
            findings.append({
                "severity": "info", "box": "9",
                "message": (
                    f"This 1098 covers {self.property_count} properties. Interest is "
                    "deductible on your main home and one other only."
                ),
            })
        return findings


def parse_1098_text(text: str) -> tuple[F1098, float, list[str]]:
    """Read a 1098 from text. Returns the form, a confidence, and warnings."""
    body = (text or "").replace(" ", " ")
    form = F1098()
    found = 0

    year = re.search(r"\b(20[12]\d)\b", body)
    if year:
        form.tax_year = int(year.group(1))

    for name, box, words in (
        ("mortgage_interest", "1", r"mortgage\s*interest\s*received"),
        ("outstanding_principal", "2", r"outstanding\s*mortgage\s*principal"),
        ("refund_of_overpaid", "4", r"refund\s*of\s*overpaid\s*interest"),
        ("mortgage_insurance", "5", r"mortgage\s*insurance\s*premiums?"),
        ("points_paid", "6", r"points\s*paid\s*on\s*purchase"),
    ):
        match = re.search(
            rf"(?:box\s*{box}\b|{words})[^0-9$]{{0,60}}{_AMOUNT}",
            body, re.IGNORECASE | re.DOTALL,
        )
        if match:
            setattr(form, name, money(match.group(1).replace(",", "")))
            found += 1

    for name, box, words in (
        ("origination_date", "3", r"mortgage\s*origination\s*date"),
        ("acquisition_date", "11", r"mortgage\s*acquisition\s*date"),
    ):
        match = re.search(
            rf"(?:box\s*{box}\b|{words})[^0-9]{{0,60}}{_DATE}", body, re.IGNORECASE | re.DOTALL
        )
        if match:
            setattr(form, name, _normalise_date(match.group(1)))
            found += 1

    count = re.search(
        r"(?:box\s*9\b|number\s*of\s*properties)[^0-9]{0,60}(\d{1,2})\b", body, re.IGNORECASE
    )
    if count:
        form.property_count = max(1, int(count.group(1)))

    lender = re.search(r"(?:recipient'?s?|lender'?s?)\s*name[^\n]*\n\s*([^\n]{3,80})",
                       body, re.IGNORECASE)
    if lender:
        form.lender = lender.group(1).strip()

    warnings = [f["message"] for f in form.validate() if f["severity"] == "warning"]
    confidence = round(min(found, 2) / 2, 3)
    if form.mortgage_interest == ZERO:
        confidence = 0.0
        warnings.append("No box 1 mortgage interest found; enter it by hand.")
    return form, confidence, warnings


def _normalise_date(raw: str) -> str:
    """US or ISO, in; ISO out. An unreadable date is dropped, not guessed."""
    raw = raw.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    parts = re.split(r"[/-]", raw)
    if len(parts) != 3:
        return ""
    month, day, year = parts
    if len(year) == 2:
        # A two-digit year on a mortgage is this century: these loans run 30
        # years and a 1990s origination would be written in full.
        year = f"20{year}"
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return ""


def to_loan(form: F1098, *, used_for: str = "purchase", is_main_home: bool = True,
            is_refinance: bool = False, term_months: int = 360):
    """Turn a parsed 1098 into the Loan the mortgage engine wants.

    `used_for` cannot come off the form -- the lender does not know what the
    money bought -- so the caller has to supply what the client said.
    """
    from taxvault.engines.mortgage import Loan

    return Loan(
        balance=form.outstanding_principal,
        interest_paid=form.mortgage_interest,
        points_paid=form.points_paid,
        mortgage_insurance=form.mortgage_insurance,
        origination=form.best_origination() or None,
        used_for=used_for,
        is_main_home=is_main_home,
        is_refinance=is_refinance,
        term_months=term_months,
        lender=form.lender,
    )
