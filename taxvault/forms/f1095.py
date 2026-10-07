"""Form 1095-A, the marketplace statement that settles a health subsidy.

Three columns matter and clients routinely read the wrong one:

  * **A** is what the plan cost.
  * **B** is the second-lowest-cost silver plan for this household -- the
    *benchmark*. The credit is measured against B, not against what they
    actually bought. Someone on a cheap bronze plan still has their credit
    computed from the silver benchmark.
  * **C** is the advance already paid to the insurer on their behalf.

Column B is the one that goes missing. A marketplace leaves it blank or at
zero when the household did not take an advance credit for a month, and
without it the credit cannot be computed at all -- the figure has to come from
the marketplace's own tax tool. Reported as a warning rather than treated as
zero, because treating a missing benchmark as zero silently wipes out the
credit.

The bottom row holds annual totals when coverage ran all twelve months, which
is the common case and what this reads. A household that changed plans
mid-year gets a second 1095-A, and both are added together.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from taxvault.money import ZERO, cents, money

_AMOUNT = r"\$?\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+\.\d{2})"
_MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)


@dataclass
class F1095A:
    marketplace: str = ""
    tax_year: int = 0
    policy_number: str = ""
    #: Column A, annual.
    annual_premium: Decimal = ZERO
    #: Column B, annual. The benchmark the credit is measured against.
    benchmark_premium: Decimal = ZERO
    #: Column C, annual. The advance already paid to the insurer.
    advance_credit: Decimal = ZERO
    months_covered: int = 0
    covered_individuals: int = 0
    state_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            key: (str(cents(value)) if isinstance(value, Decimal) else value)
            for key, value in asdict(self).items()
        }

    def to_coverage(self):
        """The engine's input type."""
        from taxvault.engines.health import MarketplaceCoverage

        return MarketplaceCoverage(
            annual_premium=self.annual_premium,
            benchmark_premium=self.benchmark_premium,
            advance_credit=self.advance_credit,
            months_covered=self.months_covered or 12,
            marketplace_state=self.state_code,
            policy_number=self.policy_number,
        )

    def validate(self) -> list[dict[str, str]]:
        findings: list[dict[str, str]] = []
        if self.annual_premium > ZERO and self.benchmark_premium <= ZERO:
            findings.append({
                "severity": "error", "box": "B",
                "message": (
                    "Column B, the second-lowest-cost silver plan, is missing or zero. "
                    "The credit is measured against that figure, not against what you "
                    "paid, so it cannot be worked out without it. A marketplace leaves "
                    "it blank when no advance credit was taken for a month -- get the "
                    "figure from the marketplace's own tax tool rather than treating it "
                    "as zero, which would wipe out the credit entirely."
                ),
            })
        if self.advance_credit > self.annual_premium > ZERO:
            findings.append({
                "severity": "warning", "box": "C",
                "message": (
                    f"The advance credit ({self.advance_credit:,.2f}) is more than the "
                    f"premium ({self.annual_premium:,.2f}), which should not happen. "
                    "One of the two columns has been read wrongly."
                ),
            })
        if 0 < self.months_covered < 12:
            findings.append({
                "severity": "info", "box": "",
                "message": (
                    f"Coverage ran {self.months_covered} month(s), so the credit is "
                    "worked out month by month rather than annually. If you changed "
                    "plans you will have a second 1095-A -- both are needed."
                ),
            })
        return findings


def parse_1095a_text(text: str) -> tuple[F1095A, float, list[str]]:
    """Read a 1095-A. Returns the form, a confidence, and warnings."""
    body = (text or "").replace(" ", " ")
    form = F1095A()
    found = 0

    year = re.search(r"\b(20[12]\d)\b", body)
    if year:
        form.tax_year = int(year.group(1))

    marketplace = re.search(
        r"(?:marketplace[^\n]{0,30}name|marketplace-assigned)[^\n]*\n\s*([^\n]{3,80})",
        body, re.IGNORECASE,
    )
    if marketplace:
        form.marketplace = marketplace.group(1).strip()

    policy = re.search(
        r"policy\s*number[^0-9A-Z]{0,20}([A-Z0-9][A-Z0-9\-]{3,24})", body, re.IGNORECASE
    )
    if policy:
        form.policy_number = policy.group(1).strip()

    # The annual row: "Annual Totals  14,400.00  15,600.00  11,000.00".
    annual = re.search(
        rf"annual\s*total[s]?[^0-9$]{{0,40}}{_AMOUNT}[^0-9$]{{1,20}}{_AMOUNT}"
        rf"[^0-9$]{{1,20}}{_AMOUNT}",
        body, re.IGNORECASE | re.DOTALL,
    )
    if annual:
        form.annual_premium = money(annual.group(1).replace(",", ""))
        form.benchmark_premium = money(annual.group(2).replace(",", ""))
        form.advance_credit = money(annual.group(3).replace(",", ""))
        found += 3
    else:
        # Column by column, where the statement labels them individually.
        for name, patterns in (
            ("annual_premium", (
                r"(?:33\b|column\s*a\b|monthly\s*enrollment\s*premium)",
            )),
            ("benchmark_premium", (
                r"(?:34\b|column\s*b\b|second\s*lowest\s*cost\s*silver)",
            )),
            ("advance_credit", (
                r"(?:35\b|column\s*c\b|monthly\s*advance\s*payment)",
            )),
        ):
            for pattern in patterns:
                match = re.search(rf"{pattern}[^0-9$]{{0,60}}{_AMOUNT}",
                                  body, re.IGNORECASE | re.DOTALL)
                if match:
                    setattr(form, name, money(match.group(1).replace(",", "")))
                    found += 1
                    break

    # Months of coverage, counted from the month rows that carry a premium.
    months = sum(
        1 for month in _MONTHS
        if re.search(rf"\b{month}\b[^0-9$\n]{{0,40}}{_AMOUNT}", body, re.IGNORECASE)
    )
    form.months_covered = months if months else (12 if form.annual_premium else 0)

    covered = re.findall(r"covered\s*individual", body, re.IGNORECASE)
    form.covered_individuals = len(covered)

    state = re.search(r"\b([A-Z]{2})\s+marketplace\b", body)
    if state:
        form.state_code = state.group(1)

    warnings = [f["message"] for f in form.validate()
                if f["severity"] in ("error", "warning")]
    confidence = round(min(found, 3) / 3, 3)
    if form.annual_premium == ZERO:
        confidence = 0.0
        warnings.append(
            "No premium figures could be read from this 1095-A. Enter columns A, B "
            "and C from the annual totals row by hand."
        )
    return form, confidence, warnings
