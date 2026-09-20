"""Form W-2: the boxes, and the arithmetic that proves they were read correctly.

The parsing here is deliberately boring. What is *not* boring is `validate()`,
which reproduces the cross-checks an experienced preparer does by eye:

* Box 4 should be exactly 6.2% of Box 3, and Box 3 never exceeds the wage base.
* Box 6 should be 1.45% of Box 5, plus 0.9% on anything over $200,000.
* Box 5 minus Box 1 is pre-tax deferral plus §125 benefits, so it should be
  explained by Box 12 codes D/E/G and Box 14 -- if it is not, a box was
  mistyped or a code is missing.
* Box 16 (state wages) usually equals Box 1, except in the states that refuse
  to follow the federal treatment of 401(k) -- New Jersey and Pennsylvania are
  the standing examples, which is why they are named rather than flagged.

Catching a transposed digit here is worth more than any downstream cleverness:
every figure in the estimate descends from these boxes.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

from taxos.config import federal
from taxos.money import ZERO, cents, money, positive

#: Box 12 codes that reduce Box 1 but not Box 3/5 (elective deferrals).
ELECTIVE_DEFERRAL_CODES = {"D", "E", "F", "G", "H", "S", "AA", "BB", "EE"}
#: Box 12 codes that are Roth -- after tax, so they do NOT reduce Box 1.
ROTH_CODES = {"AA", "BB", "EE"}
#: Pre-tax deferrals that also escape Social Security and Medicare wages.
CAFETERIA_CODES = {"W"}  # employer + employee HSA via cafeteria plan
#: States that do not allow the federal 401(k) exclusion, so Box 16 > Box 1.
STATES_TAXING_DEFERRALS = {"NJ", "PA", "MS", "AL"}


@dataclass
class W2StateLine:
    """Boxes 15-20. A W-2 can carry two, and multi-state workers hit this often."""

    state: str = ""
    state_id: str = ""
    state_wages: Decimal = ZERO          # box 16
    state_withheld: Decimal = ZERO       # box 17
    local_wages: Decimal = ZERO          # box 18
    local_withheld: Decimal = ZERO       # box 19
    locality: str = ""                   # box 20

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "state_id": self.state_id,
            "state_wages": str(self.state_wages),
            "state_withheld": str(self.state_withheld),
            "local_wages": str(self.local_wages),
            "local_withheld": str(self.local_withheld),
            "locality": self.locality,
        }


@dataclass
class W2:
    """One Form W-2, all boxes, as Decimals."""

    employer_name: str = ""
    employer_ein: str = ""
    employee_ssn: str = ""
    tax_year: int = 0

    wages: Decimal = ZERO                    # box 1  Wages, tips, other compensation
    federal_withheld: Decimal = ZERO         # box 2  Federal income tax withheld
    social_security_wages: Decimal = ZERO    # box 3
    social_security_withheld: Decimal = ZERO # box 4
    medicare_wages: Decimal = ZERO           # box 5
    medicare_withheld: Decimal = ZERO        # box 6
    social_security_tips: Decimal = ZERO     # box 7
    allocated_tips: Decimal = ZERO           # box 8
    dependent_care_benefits: Decimal = ZERO  # box 10
    nonqualified_plans: Decimal = ZERO       # box 11
    box12: dict[str, Decimal] = field(default_factory=dict)   # code -> amount
    statutory_employee: bool = False         # box 13
    retirement_plan: bool = False            # box 13
    third_party_sick_pay: bool = False       # box 13
    box14: dict[str, Decimal] = field(default_factory=dict)
    states: list[W2StateLine] = field(default_factory=list)

    # ------------------------------------------------------------------
    # derived figures
    # ------------------------------------------------------------------
    @property
    def elective_deferrals(self) -> Decimal:
        """Pre-tax retirement deferrals -- Roth codes excluded, since Roth is
        after tax and already inside Box 1."""
        return cents(
            sum(
                (amount for code, amount in self.box12.items()
                 if code in ELECTIVE_DEFERRAL_CODES and code not in ROTH_CODES),
                ZERO,
            )
        )

    @property
    def roth_deferrals(self) -> Decimal:
        return cents(sum((amount for code, amount in self.box12.items() if code in ROTH_CODES), ZERO))

    @property
    def hsa_via_employer(self) -> Decimal:
        """Box 12 code W -- employer + payroll HSA. Already excluded from Box 1,
        so it must NOT be deducted again on Form 8889."""
        return cents(self.box12.get("W", ZERO))

    @property
    def section_125_estimate(self) -> Decimal:
        """Medical/dental premiums and FSA: the part of Box 5 minus Box 1 that
        the Box 12 codes do not explain."""
        gap = money(self.medicare_wages) - money(self.wages)
        explained = self.elective_deferrals
        return positive(gap - explained) if gap > ZERO else ZERO

    @property
    def total_withheld(self) -> Decimal:
        return cents(self.federal_withheld)

    def state_withholding(self, code: str | None = None) -> Decimal:
        rows = self.states if code is None else [s for s in self.states if s.state.upper() == code.upper()]
        return cents(sum((row.state_withheld for row in rows), ZERO))

    def local_withholding(self) -> Decimal:
        return cents(sum((row.local_withheld for row in self.states), ZERO))

    @property
    def primary_state(self) -> str:
        return self.states[0].state.upper() if self.states else ""

    def fingerprint(self) -> str:
        """Identifies a duplicate upload without storing anything identifying."""
        seed = f"{self.tax_year}|{self.employer_ein}|{self.wages}|{self.federal_withheld}"
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------
    def validate(self, *, year: int | None = None) -> list[dict[str, str]]:
        """Cross-check the boxes. Returns findings, worst first; never raises.

        Severity is the point: `error` means the figures cannot all be right and
        a human must look; `warning` means it is unusual but legal.
        """
        params = federal(year or self.tax_year or None)
        findings: list[dict[str, str]] = []

        def add(severity: str, box: str, message: str) -> None:
            findings.append({"severity": severity, "box": box, "message": message})

        if self.wages < ZERO:
            add("error", "1", "Box 1 cannot be negative.")

        # --- Social Security -------------------------------------------------
        wage_base = params.amount("payroll", "social_security_wage_base")
        ss_rate = params.rate("payroll", "social_security_rate")
        if self.social_security_wages > wage_base:
            add("error", "3",
                f"Box 3 is {self.social_security_wages:,.2f}, above the {params.year} "
                f"Social Security wage base of {wage_base:,.0f}. One employer cannot exceed it.")
        if self.social_security_wages > ZERO:
            expected = cents((self.social_security_wages + self.social_security_tips) * ss_rate)
            if abs(expected - self.social_security_withheld) > Decimal("1.00"):
                add("error", "4",
                    f"Box 4 should be about {expected:,.2f} (6.2% of Box 3 + Box 7) "
                    f"but reads {self.social_security_withheld:,.2f}.")

        # --- Medicare --------------------------------------------------------
        med_rate = params.rate("payroll", "medicare_rate")
        add_rate = params.rate("payroll", "additional_medicare_rate")
        if self.medicare_wages > ZERO:
            expected = cents(self.medicare_wages * med_rate)
            # Additional Medicare is withheld by the employer above $200,000
            # regardless of filing status -- that is the employer's rule.
            surcharge_base = positive(self.medicare_wages - money(200000))
            expected += cents(surcharge_base * add_rate)
            if abs(expected - self.medicare_withheld) > Decimal("1.00"):
                add("error", "6",
                    f"Box 6 should be about {expected:,.2f} (1.45% of Box 5, plus 0.9% "
                    f"over 200,000) but reads {self.medicare_withheld:,.2f}.")

        # --- Box 1 versus Box 5 ---------------------------------------------
        gap = money(self.medicare_wages) - money(self.wages)
        if gap < ZERO and self.medicare_wages > ZERO:
            add("warning", "1",
                "Box 1 exceeds Box 5. That happens with non-qualified deferred "
                "compensation or certain equity, but is worth confirming.")
        elif gap > ZERO:
            unexplained = gap - self.elective_deferrals
            if unexplained > Decimal("1.00") and not self.section_125_estimate:
                add("warning", "12",
                    f"Box 5 exceeds Box 1 by {gap:,.2f}, of which {self.elective_deferrals:,.2f} "
                    "is explained by Box 12. The rest is usually pre-tax health premiums "
                    "or an FSA -- confirm nothing was missed.")

        # --- deferral limits -------------------------------------------------
        limit = params.amount("contribution_limits", "elective_deferral_401k")
        catch_up = params.amount("contribution_limits", "catch_up_401k")
        deferred = self.elective_deferrals + self.roth_deferrals
        if deferred > limit + catch_up:
            add("error", "12",
                f"Box 12 deferrals total {deferred:,.2f}, above the {params.year} limit of "
                f"{limit:,.0f} plus catch-up. An excess deferral must be withdrawn by "
                "April 15 or it is taxed twice.")
        elif deferred > limit:
            add("warning", "12",
                f"Deferrals of {deferred:,.2f} exceed the base limit of {limit:,.0f}; "
                "this is only allowed if the employee is 50 or older.")

        # --- retirement box --------------------------------------------------
        if deferred > ZERO and not self.retirement_plan:
            add("warning", "13",
                "Box 12 shows a retirement deferral but the Box 13 'Retirement plan' "
                "tick is clear. That tick controls whether an IRA deduction is limited.")

        # --- state lines -----------------------------------------------------
        for line in self.states:
            code = (line.state or "").upper()
            if not code:
                continue
            if line.state_wages > ZERO and line.state_withheld > line.state_wages:
                add("error", "17", f"{code}: Box 17 withholding exceeds Box 16 wages.")
            difference = money(line.state_wages) - money(self.wages)
            if abs(difference) > Decimal("1.00"):
                if code in STATES_TAXING_DEFERRALS:
                    add("info", "16",
                        f"{code}: Box 16 differs from Box 1 by {difference:,.2f}, which is "
                        f"expected -- {code} does not allow the federal 401(k) exclusion.")
                elif difference != ZERO:
                    add("warning", "16",
                        f"{code}: Box 16 of {line.state_wages:,.2f} differs from Box 1 of "
                        f"{self.wages:,.2f}. Confirm this is a part-year or multi-state W-2.")

        if len(self.states) > 1:
            add("info", "15",
                f"This W-2 reports wages to {len(self.states)} states. A part-year or "
                "non-resident return is likely needed for each.")

        order = {"error": 0, "warning": 1, "info": 2}
        return sorted(findings, key=lambda f: order[f["severity"]])

    # ------------------------------------------------------------------
    # serialisation
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "employer_name": self.employer_name,
            "tax_year": self.tax_year,
            "box1_wages": str(self.wages),
            "box2_federal_withheld": str(self.federal_withheld),
            "box3_social_security_wages": str(self.social_security_wages),
            "box4_social_security_withheld": str(self.social_security_withheld),
            "box5_medicare_wages": str(self.medicare_wages),
            "box6_medicare_withheld": str(self.medicare_withheld),
            "box7_social_security_tips": str(self.social_security_tips),
            "box8_allocated_tips": str(self.allocated_tips),
            "box10_dependent_care": str(self.dependent_care_benefits),
            "box11_nonqualified": str(self.nonqualified_plans),
            "box12": {code: str(amount) for code, amount in self.box12.items()},
            "box13": {
                "statutory_employee": self.statutory_employee,
                "retirement_plan": self.retirement_plan,
                "third_party_sick_pay": self.third_party_sick_pay,
            },
            "box14": {label: str(amount) for label, amount in self.box14.items()},
            "states": [line.to_dict() for line in self.states],
            "derived": {
                "elective_deferrals": str(self.elective_deferrals),
                "roth_deferrals": str(self.roth_deferrals),
                "hsa_via_employer": str(self.hsa_via_employer),
                "section_125_estimate": str(self.section_125_estimate),
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "W2":
        """Build from either the API's box names or this class's own output."""

        def pick(*names: str) -> Decimal:
            for name in names:
                if name in data and data[name] not in (None, ""):
                    return money(data[name])
            return ZERO

        box12_raw = data.get("box12") or {}
        box14_raw = data.get("box14") or {}
        box13 = data.get("box13") or {}
        states = [
            W2StateLine(
                state=(row.get("state") or "").upper(),
                state_id=row.get("state_id", ""),
                state_wages=money(row.get("state_wages", 0)),
                state_withheld=money(row.get("state_withheld", 0)),
                local_wages=money(row.get("local_wages", 0)),
                local_withheld=money(row.get("local_withheld", 0)),
                locality=row.get("locality", ""),
            )
            for row in (data.get("states") or [])
        ]
        return cls(
            employer_name=data.get("employer_name", ""),
            employer_ein=data.get("employer_ein", ""),
            employee_ssn=data.get("employee_ssn", ""),
            tax_year=int(data.get("tax_year") or 0),
            wages=pick("box1_wages", "wages", "box1"),
            federal_withheld=pick("box2_federal_withheld", "federal_withheld", "box2"),
            social_security_wages=pick("box3_social_security_wages", "social_security_wages", "box3"),
            social_security_withheld=pick("box4_social_security_withheld", "social_security_withheld", "box4"),
            medicare_wages=pick("box5_medicare_wages", "medicare_wages", "box5"),
            medicare_withheld=pick("box6_medicare_withheld", "medicare_withheld", "box6"),
            social_security_tips=pick("box7_social_security_tips", "social_security_tips", "box7"),
            allocated_tips=pick("box8_allocated_tips", "allocated_tips", "box8"),
            dependent_care_benefits=pick("box10_dependent_care", "dependent_care_benefits", "box10"),
            nonqualified_plans=pick("box11_nonqualified", "nonqualified_plans", "box11"),
            box12={str(code).upper(): money(amount) for code, amount in box12_raw.items()},
            statutory_employee=bool(box13.get("statutory_employee", data.get("statutory_employee", False))),
            retirement_plan=bool(box13.get("retirement_plan", data.get("retirement_plan", False))),
            third_party_sick_pay=bool(box13.get("third_party_sick_pay", False)),
            box14={str(label): money(amount) for label, amount in box14_raw.items()},
            states=states,
        )


# ---------------------------------------------------------------------------
# text extraction
# ---------------------------------------------------------------------------
_MONEY = r"([0-9][0-9,]*\.?[0-9]{0,2})"
_BOX_PATTERNS: list[tuple[str, str]] = [
    ("wages", rf"(?:box\s*1\b|wages,?\s*tips,?\s*other\s*comp\w*)[^0-9$]{{0,40}}\$?\s*{_MONEY}"),
    ("federal_withheld", rf"(?:box\s*2\b|federal\s*income\s*tax\s*withheld)[^0-9$]{{0,40}}\$?\s*{_MONEY}"),
    ("social_security_wages", rf"(?:box\s*3\b|social\s*security\s*wages)[^0-9$]{{0,40}}\$?\s*{_MONEY}"),
    ("social_security_withheld", rf"(?:box\s*4\b|social\s*security\s*tax\s*withheld)[^0-9$]{{0,40}}\$?\s*{_MONEY}"),
    ("medicare_wages", rf"(?:box\s*5\b|medicare\s*wages\s*and\s*tips)[^0-9$]{{0,40}}\$?\s*{_MONEY}"),
    ("medicare_withheld", rf"(?:box\s*6\b|medicare\s*tax\s*withheld)[^0-9$]{{0,40}}\$?\s*{_MONEY}"),
    ("social_security_tips", rf"(?:box\s*7\b|social\s*security\s*tips)[^0-9$]{{0,40}}\$?\s*{_MONEY}"),
    ("allocated_tips", rf"(?:box\s*8\b|allocated\s*tips)[^0-9$]{{0,40}}\$?\s*{_MONEY}"),
    ("dependent_care_benefits", rf"(?:box\s*10\b|dependent\s*care\s*benefits)[^0-9$]{{0,40}}\$?\s*{_MONEY}"),
]


def parse_w2_text(text: str) -> tuple[W2, float, list[str]]:
    """Best-effort extraction from pasted or OCR'd W-2 text.

    Returns the form, a confidence between 0 and 1, and the warnings. The
    confidence is the fraction of the boxes that matter which were actually
    found -- it exists so the UI can route a poor read to manual review instead
    of quietly estimating on half a form. This is not an OCR engine: it reads
    text that already exists. A scanned image needs a real OCR pass first, and
    `parse_confidence` is how that gets flagged.
    """
    warnings: list[str] = []
    body = (text or "").replace(" ", " ")
    lowered = body.lower()
    found: dict[str, Decimal] = {}

    for field_name, pattern in _BOX_PATTERNS:
        match = re.search(pattern, lowered, re.IGNORECASE | re.DOTALL)
        if match:
            found[field_name] = money(match.group(1).replace(",", ""))

    form = W2(**found)  # type: ignore[arg-type]

    year_match = re.search(r"\b(20[12]\d)\b", body)
    if year_match:
        form.tax_year = int(year_match.group(1))
    else:
        warnings.append("No tax year found on the form; confirm which year this W-2 is for.")

    employer = re.search(r"(?:employer'?s?\s*name[^\n]*\n)([^\n]{3,80})", lowered)
    if employer:
        form.employer_name = employer.group(1).strip().title()

    ein = re.search(r"\b(\d{2}-\d{7})\b", body)
    if ein:
        form.employer_ein = ein.group(1)

    for code, amount in re.findall(r"\b(?:box\s*12\w*\s*)?([A-Z]{1,2})\s*[:\-]?\s*\$?\s*([0-9][0-9,]*\.?[0-9]{0,2})",
                                   body):
        if code.upper() in ELECTIVE_DEFERRAL_CODES | CAFETERIA_CODES | {"C", "P", "T", "DD"}:
            form.box12[code.upper()] = money(amount.replace(",", ""))

    for state, wages, withheld in re.findall(
        rf"\b([A-Z]{{2}})\s+[\w\-/]*\s*{_MONEY}\s+{_MONEY}", body
    ):
        if state in _US_STATES:
            form.states.append(
                W2StateLine(
                    state=state,
                    state_wages=money(wages.replace(",", "")),
                    state_withheld=money(withheld.replace(",", "")),
                )
            )
            break  # one state line per pass; extras go through manual entry

    critical = ["wages", "federal_withheld", "social_security_wages", "medicare_wages"]
    confidence = round(sum(1 for name in critical if name in found) / len(critical), 3)
    if confidence < 1.0:
        missing = [name for name in critical if name not in found]
        warnings.append(
            "Could not read: " + ", ".join(missing) + ". Enter these boxes by hand."
        )
    return form, confidence, warnings


_US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","DC","FL","GA","HI","ID","IL","IN","IA","KS","KY","LA",
    "ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK","OR",
    "PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY",
}


def combine(forms: Iterable[W2]) -> dict[str, Decimal]:
    """Totals across every W-2 a taxpayer holds for the year."""
    forms = list(forms)
    return {
        "wages": cents(sum((f.wages for f in forms), ZERO)),
        "federal_withheld": cents(sum((f.federal_withheld for f in forms), ZERO)),
        "social_security_wages": cents(sum((f.social_security_wages for f in forms), ZERO)),
        "social_security_withheld": cents(sum((f.social_security_withheld for f in forms), ZERO)),
        "medicare_wages": cents(sum((f.medicare_wages for f in forms), ZERO)),
        "medicare_withheld": cents(sum((f.medicare_withheld for f in forms), ZERO)),
        "tips": cents(sum((f.social_security_tips + f.allocated_tips for f in forms), ZERO)),
        "dependent_care_benefits": cents(sum((f.dependent_care_benefits for f in forms), ZERO)),
        "elective_deferrals": cents(sum((f.elective_deferrals for f in forms), ZERO)),
        "roth_deferrals": cents(sum((f.roth_deferrals for f in forms), ZERO)),
        "hsa_via_employer": cents(sum((f.hsa_via_employer for f in forms), ZERO)),
        "state_withheld": cents(sum((f.state_withholding() for f in forms), ZERO)),
        "local_withheld": cents(sum((f.local_withholding() for f in forms), ZERO)),
    }


def excess_social_security(forms: Iterable[W2], *, year: int) -> Decimal:
    """Over-withheld Social Security across multiple employers is refundable.

    One employer cannot over-withhold (that is their error to fix), but two
    employers each withholding up to the wage base routinely produces a real
    refundable credit on Schedule 3. It is missed constantly on self-prepared
    returns, so it is computed rather than left to the client to notice.
    """
    forms = list(forms)
    if len(forms) < 2:
        return ZERO
    params = federal(year)
    ceiling = cents(
        params.amount("payroll", "social_security_wage_base")
        * params.rate("payroll", "social_security_rate")
    )
    withheld = cents(sum((f.social_security_withheld for f in forms), ZERO))
    return positive(withheld - ceiling)
