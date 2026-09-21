"""Reading the 1099 family: the B, the DIV and the R.

Three forms, three different jobs, and the difference between them is the
difference between a 15% tax and a 37% one:

  * **1099-B** -- every sale in a taxable brokerage account. It is the only
    one of the three that carries a holding period, which is why it is the
    only one that can tell you whether a gain is short-term or long-term.
  * **1099-DIV** -- dividends, split into ordinary and qualified, plus the
    capital gain distributions a fund passes through. Those distributions are
    ALWAYS long-term however long you held the fund, which catches out anyone
    who bought in November.
  * **1099-R** -- money leaving a 401(k), 403(b), IRA or pension. Box 7 is the
    important one: a single letter decides whether it is taxable at all and
    whether the 10% additional tax applies.

A brokerage almost never sends three separate documents. It sends a
"consolidated 1099" of thirty pages with all of them inside, and the numbers
worth having are in the summary pages at the front, not in the transaction
detail. So these parsers look for section totals first and individual rows
second.

There is no OCR here. A photographed statement reads as nothing and is
reported at zero confidence rather than guessed at -- an invented cost basis
is worse than a blank field, because a blank field gets asked about.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from taxvault.money import ZERO, cents, money, positive

#: An amount that looks like money: carries cents, a thousands separator, or a
#: leading minus. A bare integer in form furniture ("Form 1099-B", "2026") is
#: not a figure, and treating it as one is how a tax year becomes a dividend.
_AMOUNT = r"\(?-?\$?\s*(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+\.\d{2})\)?"


def _to_decimal(raw: str, negative: bool = False) -> Decimal:
    value = money((raw or "0").replace(",", "").replace("$", "").strip())
    return -value if negative else value


def _find(body: str, *patterns: str) -> Decimal | None:
    """First match of any pattern, as a Decimal. Parenthesised means negative."""
    for pattern in patterns:
        match = re.search(pattern, body, re.IGNORECASE | re.DOTALL)
        if match:
            whole = match.group(0)
            negative = whole.strip().startswith("(") or "-" in whole.split(match.group(1))[0][-2:]
            return _to_decimal(match.group(1), negative)
    return None


def _label(box: str, *words: str) -> str:
    """A pattern matching either the box number or its printed wording."""
    alternatives = [rf"box\s*{re.escape(box)}\b"] + [w for w in words]
    return rf"(?:{'|'.join(alternatives)})[^0-9$(\-]{{0,60}}{_AMOUNT}"


# ===========================================================================
# 1099-B: sales
# ===========================================================================
@dataclass
class F1099B:
    """A consolidated 1099-B, summarised the way Schedule D needs it."""

    payer: str = ""
    tax_year: int = 0
    short_term_proceeds: Decimal = ZERO
    short_term_basis: Decimal = ZERO
    short_term_gain: Decimal = ZERO       # may be negative
    long_term_proceeds: Decimal = ZERO
    long_term_basis: Decimal = ZERO
    long_term_gain: Decimal = ZERO        # may be negative
    wash_sale_disallowed: Decimal = ZERO  # box 1g
    federal_withheld: Decimal = ZERO      # box 4
    basis_not_reported: bool = False

    def net_gain(self) -> Decimal:
        return cents(self.short_term_gain + self.long_term_gain)

    def to_dict(self) -> dict[str, Any]:
        return {
            key: (str(cents(value)) if isinstance(value, Decimal) else value)
            for key, value in asdict(self).items()
        }


def parse_1099b_text(text: str) -> tuple[F1099B, float, list[str]]:
    body = (text or "").replace(" ", " ")
    form = F1099B()
    warnings: list[str] = []
    found = 0

    year = re.search(r"\b(20[12]\d)\b", body)
    if year:
        form.tax_year = int(year.group(1))

    # Section totals, which is how every consolidated statement prints it.
    pairs = (
        ("short_term_proceeds", (rf"short[\s\-]*term[^\n]{{0,80}}?proceeds[^0-9$(\-]{{0,40}}{_AMOUNT}",
                                 rf"total\s+short[\s\-]*term[^0-9$(\-]{{0,40}}{_AMOUNT}")),
        ("short_term_basis", (rf"short[\s\-]*term[^\n]{{0,80}}?(?:cost|basis)[^0-9$(\-]{{0,40}}{_AMOUNT}",)),
        ("short_term_gain", (rf"short[\s\-]*term[^\n]{{0,80}}?(?:gain|loss)[^0-9$(\-]{{0,40}}{_AMOUNT}",)),
        ("long_term_proceeds", (rf"long[\s\-]*term[^\n]{{0,80}}?proceeds[^0-9$(\-]{{0,40}}{_AMOUNT}",
                                rf"total\s+long[\s\-]*term[^0-9$(\-]{{0,40}}{_AMOUNT}")),
        ("long_term_basis", (rf"long[\s\-]*term[^\n]{{0,80}}?(?:cost|basis)[^0-9$(\-]{{0,40}}{_AMOUNT}",)),
        ("long_term_gain", (rf"long[\s\-]*term[^\n]{{0,80}}?(?:gain|loss)[^0-9$(\-]{{0,40}}{_AMOUNT}",)),
        ("wash_sale_disallowed", (_label("1g", r"wash\s*sale\s*loss\s*disallowed"),)),
        ("federal_withheld", (_label("4", r"federal\s*income\s*tax\s*withheld"),)),
    )
    for name, patterns in pairs:
        value = _find(body, *patterns)
        if value is not None:
            setattr(form, name, value)
            found += 1

    # Where only proceeds and basis are stated, the gain is the difference.
    if form.short_term_gain == ZERO and form.short_term_proceeds:
        form.short_term_gain = cents(form.short_term_proceeds - form.short_term_basis)
    if form.long_term_gain == ZERO and form.long_term_proceeds:
        form.long_term_gain = cents(form.long_term_proceeds - form.long_term_basis)

    if re.search(r"basis\s+not\s+reported|noncovered|non-covered", body, re.IGNORECASE):
        form.basis_not_reported = True
        warnings.append(
            "Some positions are marked as noncovered, meaning the broker did not report "
            "the cost basis to the IRS. You have to supply it yourself, from your own "
            "records -- and a missing basis is treated as ZERO, which taxes the entire "
            "proceeds as gain."
        )
    if form.wash_sale_disallowed > ZERO:
        warnings.append(
            f"{form.wash_sale_disallowed:,.2f} of loss is disallowed under the wash-sale "
            "rule. It is added to the basis of the replacement shares, not lost."
        )
    confidence = round(min(found, 4) / 4, 3)
    if confidence < 1.0:
        warnings.append(
            "Could not read every section total from this 1099-B. Check the short-term "
            "and long-term gain figures against the summary page before relying on them."
        )
    return form, confidence, warnings


# ===========================================================================
# 1099-DIV: dividends
# ===========================================================================
@dataclass
class F1099DIV:
    payer: str = ""
    tax_year: int = 0
    ordinary_dividends: Decimal = ZERO          # box 1a
    qualified_dividends: Decimal = ZERO         # box 1b, a SUBSET of 1a
    capital_gain_distributions: Decimal = ZERO  # box 2a, always long-term
    unrecaptured_1250: Decimal = ZERO           # box 2b
    collectibles_gain: Decimal = ZERO           # box 2d
    nondividend_distributions: Decimal = ZERO   # box 3, a return of capital
    federal_withheld: Decimal = ZERO            # box 4
    section_199a_dividends: Decimal = ZERO      # box 5
    foreign_tax_paid: Decimal = ZERO            # box 7
    exempt_interest_dividends: Decimal = ZERO   # box 12

    def to_dict(self) -> dict[str, Any]:
        return {
            key: (str(cents(value)) if isinstance(value, Decimal) else value)
            for key, value in asdict(self).items()
        }

    def validate(self) -> list[dict[str, str]]:
        findings: list[dict[str, str]] = []
        if self.qualified_dividends > self.ordinary_dividends:
            findings.append({
                "severity": "error", "box": "1b",
                "message": (
                    f"Qualified dividends ({self.qualified_dividends:,.2f}) cannot exceed "
                    f"total ordinary dividends ({self.ordinary_dividends:,.2f}) -- box 1b "
                    "is a subset of box 1a, not an addition to it."
                ),
            })
        if self.nondividend_distributions > ZERO:
            findings.append({
                "severity": "info", "box": "3",
                "message": (
                    f"{self.nondividend_distributions:,.2f} is a return of your own capital, "
                    "not income. It is not taxed now -- it reduces your cost basis, so it "
                    "raises the gain when you eventually sell. Keep the figure."
                ),
            })
        return findings


def parse_1099div_text(text: str) -> tuple[F1099DIV, float, list[str]]:
    body = (text or "").replace(" ", " ")
    form = F1099DIV()
    found = 0
    year = re.search(r"\b(20[12]\d)\b", body)
    if year:
        form.tax_year = int(year.group(1))

    for name, box, *words in (
        ("ordinary_dividends", "1a", r"total\s*ordinary\s*dividends"),
        ("qualified_dividends", "1b", r"qualified\s*dividends"),
        ("capital_gain_distributions", "2a", r"total\s*capital\s*gain\s*distr\w*"),
        ("unrecaptured_1250", "2b", r"unrecap\w*\s*sec\w*\s*1250\s*gain"),
        ("collectibles_gain", "2d", r"collectibles\s*\(?28"),
        ("nondividend_distributions", "3", r"nondividend\s*distributions"),
        ("federal_withheld", "4", r"federal\s*income\s*tax\s*withheld"),
        ("section_199a_dividends", "5", r"section\s*199a\s*dividends"),
        ("foreign_tax_paid", "7", r"foreign\s*tax\s*paid"),
        ("exempt_interest_dividends", "12", r"exempt[\s\-]*interest\s*dividends"),
    ):
        value = _find(body, _label(box, *words))
        if value is not None:
            setattr(form, name, value)
            found += 1

    warnings = [f["message"] for f in form.validate() if f["severity"] == "error"]
    confidence = round(min(found, 3) / 3, 3)
    if form.ordinary_dividends == ZERO:
        warnings.append("No box 1a total found; enter the ordinary dividends by hand.")
    return form, confidence, warnings


# ===========================================================================
# 1099-R: retirement distributions
# ===========================================================================
@dataclass
class F1099R:
    payer: str = ""
    tax_year: int = 0
    gross_distribution: Decimal = ZERO       # box 1
    taxable_amount: Decimal | None = None    # box 2a
    taxable_not_determined: bool = False     # box 2b
    total_distribution: bool = False         # box 2b
    capital_gain: Decimal = ZERO             # box 3
    federal_withheld: Decimal = ZERO         # box 4
    employee_contributions: Decimal = ZERO   # box 5: basis already taxed
    net_unrealized_appreciation: Decimal = ZERO  # box 6
    distribution_code: str = ""              # box 7
    is_ira: bool = False                     # the IRA/SEP/SIMPLE checkbox
    state_withheld: Decimal = ZERO           # box 14
    state_code: str = ""

    def plan_kind(self) -> str:
        return "ira" if self.is_ira else "401k"

    def to_dict(self) -> dict[str, Any]:
        out = {
            key: (str(cents(value)) if isinstance(value, Decimal) else value)
            for key, value in asdict(self).items()
        }
        out["taxable_amount"] = (
            None if self.taxable_amount is None else str(cents(self.taxable_amount))
        )
        out["plan_kind"] = self.plan_kind()
        return out

    def validate(self) -> list[dict[str, str]]:
        findings: list[dict[str, str]] = []
        taxable = self.gross_distribution if self.taxable_amount is None else self.taxable_amount
        if taxable > self.gross_distribution:
            findings.append({
                "severity": "error", "box": "2a",
                "message": (
                    f"The taxable amount ({taxable:,.2f}) is more than the gross "
                    f"distribution ({self.gross_distribution:,.2f}). One of the two has "
                    "been read wrongly."
                ),
            })
        if self.taxable_not_determined:
            findings.append({
                "severity": "warning", "box": "2b",
                "message": (
                    "Box 2b says the payer could not determine the taxable amount. That is "
                    "normal for an IRA and it does NOT mean the whole distribution is "
                    "taxable -- if you ever made a non-deductible contribution, part of "
                    "this is your own money coming back. Form 8606 works it out, and "
                    "without it you pay tax twice on the same dollars."
                ),
            })
        code = (self.distribution_code or "").upper()
        if "1" in code:
            findings.append({
                "severity": "warning", "box": "7",
                "message": (
                    "Code 1 means an early distribution with no exception known to the "
                    "payer. The 10% additional tax applies unless you claim an exception "
                    "on Form 5329 -- the payer does not know about most of them, so the "
                    "code being 1 does not settle it."
                ),
            })
        if "G" in code or "H" in code:
            findings.append({
                "severity": "info", "box": "7",
                "message": (
                    "Code G is a direct rollover: none of it is taxable. It still has to "
                    "appear on the return, because the IRS has the same form."
                ),
            })
        if self.net_unrealized_appreciation > ZERO:
            findings.append({
                "severity": "info", "box": "6",
                "message": (
                    f"Box 6 shows {self.net_unrealized_appreciation:,.2f} of net unrealised "
                    "appreciation on employer stock. Handled correctly, that growth is "
                    "taxed at long-term capital gains rates instead of ordinary rates -- "
                    "one of the few ways money leaves a 401(k) without ordinary treatment. "
                    "It needs a lump-sum distribution and it is easy to forfeit."
                ),
            })
        return findings


def parse_1099r_text(text: str) -> tuple[F1099R, float, list[str]]:
    body = (text or "").replace(" ", " ")
    form = F1099R()
    found = 0
    year = re.search(r"\b(20[12]\d)\b", body)
    if year:
        form.tax_year = int(year.group(1))

    for name, box, *words in (
        ("gross_distribution", "1", r"gross\s*distribution"),
        ("capital_gain", "3", r"capital\s*gain"),
        ("federal_withheld", "4", r"federal\s*income\s*tax\s*withheld"),
        ("employee_contributions", "5", r"employee\s*contributions?"),
        ("net_unrealized_appreciation", "6", r"net\s*unrealized\s*appreciation"),
        ("state_withheld", "14", r"state\s*tax\s*withheld"),
    ):
        value = _find(body, _label(box, *words))
        if value is not None:
            setattr(form, name, value)
            found += 1

    taxable = _find(body, _label("2a", r"taxable\s*amount"))
    if taxable is not None:
        form.taxable_amount = taxable
        found += 1

    if re.search(r"taxable\s*amount\s*not\s*determined", body, re.IGNORECASE):
        form.taxable_not_determined = True
    if re.search(r"total\s*distribution", body, re.IGNORECASE):
        form.total_distribution = True
    if re.search(r"IRA\s*/?\s*SEP\s*/?\s*SIMPLE", body, re.IGNORECASE):
        form.is_ira = True

    code = re.search(
        r"(?:box\s*7\b|distribution\s*code\(?s?\)?)[^A-Z0-9]{0,40}([1-9A-Z]{1,2})\b",
        body, re.IGNORECASE,
    )
    if code:
        form.distribution_code = code.group(1).upper()
        found += 1

    state = re.search(r"\b([A-Z]{2})\b(?=[^\n]{0,30}state)", body)
    if state:
        form.state_code = state.group(1)

    warnings = [f["message"] for f in form.validate() if f["severity"] in ("error", "warning")]
    confidence = round(min(found, 3) / 3, 3)
    if form.gross_distribution == ZERO:
        warnings.append("No box 1 gross distribution found; enter it by hand.")
    if not form.distribution_code:
        warnings.append(
            "No box 7 distribution code found. That single letter decides whether this is "
            "taxable at all and whether the 10% additional tax applies, so it cannot be "
            "guessed -- take it from the form."
        )
    return form, confidence, warnings


# ===========================================================================
# Which form is this?
# ===========================================================================
_SIGNATURES = (
    ("1099_r", (r"1099[\s\-]*R\b", r"gross\s*distribution", r"distribution\s*code")),
    ("1099_b", (r"1099[\s\-]*B\b", r"proceeds\s*from\s*broker", r"wash\s*sale")),
    ("1099_div", (r"1099[\s\-]*DIV\b", r"ordinary\s*dividends", r"capital\s*gain\s*distr")),
    ("1099_int", (r"1099[\s\-]*INT\b", r"interest\s*income")),
    ("1098", (r"\b1098\b(?![\s\-]*[ET])", r"mortgage\s*interest\s*received")),
    ("w2", (r"\bW-?2\b", r"wages,?\s*tips,?\s*other")),
)


def detect_form_kind(text: str) -> tuple[str, float]:
    """Guess which form this is, and how sure we are.

    A consolidated brokerage statement matches several at once, which is
    correct -- it contains several. The caller runs every parser that scores
    and keeps what each one finds.
    """
    body = (text or "")
    best, best_score = "other", 0.0
    for kind, patterns in _SIGNATURES:
        hits = sum(1 for p in patterns if re.search(p, body, re.IGNORECASE))
        score = hits / len(patterns)
        if score > best_score:
            best, best_score = kind, score
    return best, round(best_score, 3)


def kinds_present(text: str) -> list[str]:
    """Every form this document appears to contain, for a consolidated 1099."""
    body = (text or "")
    out = []
    for kind, patterns in _SIGNATURES:
        if sum(1 for p in patterns if re.search(p, body, re.IGNORECASE)) >= 2:
            out.append(kind)
    return out
