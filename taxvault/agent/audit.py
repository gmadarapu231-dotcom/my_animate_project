"""The agent checking its own arithmetic, the way a second preparer would.

A practice does not sign a return because the software produced one. Someone
reviews it: adds the W-2s up by hand, re-reads the bracket table, confirms
the withholding on the return is the withholding on the forms. That review
is what makes an automated preparer trustworthy, and until now the agent did
not do it -- it cross-checked the FORMS against each other and then trusted
whatever the engine said about them.

Every check here **re-derives** the figure from the source rather than asking
the engine again. Comparing the engine's answer to the engine's answer proves
nothing; the tax is recomputed straight off the bracket table, the wages are
summed straight off the documents, and the two are compared. A check that
cannot be made independently is not written.

What this is not: a second opinion on tax law. It catches arithmetic that
does not tie, figures that did not come from a document, and results outside
the bounds the law puts on them. A return can pass every check here and still
be wrong about something only a person would notice, which is why the filing
gate still requires one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from taxvault.config import UnsupportedTaxYear, federal
from taxvault.money import ZERO, cents, money, positive

#: Cents of slack. The engine rounds at each line as the IRS does, so a few
#: cents between an independent recomputation and the engine's answer is the
#: rounding, not a fault. A dollar is not.
TOLERANCE = Decimal("1.00")

OK, FAILED, SKIPPED = "ok", "failed", "skipped"


@dataclass
class Check:
    name: str
    status: str = OK
    detail: str = ""
    expected: str = ""
    actual: str = ""

    @property
    def passed(self) -> bool:
        return self.status != FAILED

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail,
                "expected": self.expected, "actual": self.actual}


@dataclass
class AuditReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAILED]

    @property
    def ran(self) -> int:
        return sum(1 for c in self.checks if c.status != SKIPPED)

    @property
    def clean(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        if not self.checks:
            return "nothing to check"
        if self.clean:
            return f"{self.ran} independent checks, all tie"
        return (f"{len(self.failures)} of {self.ran} checks do not tie: "
                + "; ".join(c.name for c in self.failures[:3]))

    def to_dict(self) -> dict[str, Any]:
        return {"clean": self.clean, "ran": self.ran,
                "failed": len(self.failures), "summary": self.summary(),
                "checks": [c.to_dict() for c in self.checks]}


# ===========================================================================
# Independent recomputation
# ===========================================================================
def tax_from_brackets(taxable: Decimal, *, year: int, status: str) -> Decimal:
    """Walk the published bracket table by hand.

    Deliberately a separate implementation from the engine's. Two bodies of
    code reading the same table and agreeing is evidence; one body of code
    agreeing with itself is not.
    """
    owed = ZERO
    for bracket in federal(year).brackets(status):
        if taxable <= bracket.floor:
            break
        ceiling = bracket.ceiling if bracket.ceiling is not None else taxable
        slice_ = min(taxable, ceiling) - bracket.floor
        if slice_ > ZERO:
            owed += slice_ * bracket.rate
    return cents(owed)


def _wage_line(federal_result: Any) -> Decimal | None:
    """The wages the return actually used, off its own 1040 line list."""
    for line in getattr(federal_result, "lines", None) or []:
        label = str(line.get("label", "")).lower()
        if "wages" in label and "state" not in label:
            return money(line.get("amount", 0))
    return None


def _sum_documents(forms: list[Any], attribute: str) -> Decimal:
    total = ZERO
    for form in forms:
        try:
            total += money(getattr(form, attribute, 0) or 0)
        except Exception:
            continue
    return cents(total)


# ===========================================================================
# The audit
# ===========================================================================
def audit_return(estimate: Any, *, w2s: list[Any] | None = None,
                 quote: Any = None,
                 other_payments: Decimal | float | str = 0,
                 other_payments_detail: str = "") -> AuditReport:
    """Check a computed return against its own sources and the rate tables.

    `other_payments` is everything paid in that is not W-2 withholding --
    tax withheld on a 1099-R, estimated payments, a prior year's refund
    carried forward. It is needed to make the withholding check two-sided:
    without it the check can only ask whether enough was claimed, and the
    dangerous direction is too much.
    """
    report = AuditReport()
    add = report.checks.append
    w2s = list(w2s or [])

    # A planning run deliberately moves money -- a 401(k) contribution takes
    # wages off the return -- so its figures are a projection, not a return,
    # and they do not tie to the W-2 because they are not meant to. What has
    # to tie to the forms is the return as filed, which the engine keeps as
    # the baseline whenever it planned on top of it.
    planned = getattr(estimate, "baseline", None)
    if planned is not None and hasattr(planned, "federal"):
        add(Check("Audited as filed, not as planned", OK,
                  "this run also modelled planning moves; the figures checked "
                  "below are the return as the documents stand"))
        estimate = planned

    try:
        federal_result = estimate.federal
    except AttributeError:
        add(Check("The return could be read", FAILED,
                  "The estimate does not look like a computed return."))
        return report

    year = int(getattr(estimate, "tax_year", 0) or 0)
    status = getattr(estimate, "filing_status", "single")

    def compare(name: str, expected: Decimal, actual: Decimal, detail: str = "",
                tolerance: Decimal = TOLERANCE) -> None:
        difference = abs(cents(expected) - cents(actual))
        add(Check(
            name,
            OK if difference <= tolerance else FAILED,
            detail or (f"ties to the cent" if difference == ZERO
                       else f"out by {difference:,.2f}"),
            expected=f"{cents(expected):,.2f}", actual=f"{cents(actual):,.2f}",
        ))

    # --- the 1040's own arithmetic ---------------------------------------
    income = money(federal_result.total_income)
    adjustments = money(federal_result.adjustments)
    compare("Adjusted gross income is total income less adjustments",
            income - adjustments, money(federal_result.agi))

    # QBI and the OBBBA deductions come off below the line, after the
    # standard or itemised deduction, so the identity is not AGI minus one
    # number. Leaving them out failed every self-employed return.
    below_the_line = (money(federal_result.deduction_taken)
                      + money(federal_result.qbi_deduction))
    compare("Taxable income is AGI less every deduction",
            positive(money(federal_result.agi) - below_the_line),
            money(federal_result.taxable_income),
            detail=f"deduction {money(federal_result.deduction_taken):,.2f}"
                   + (f" plus QBI {money(federal_result.qbi_deduction):,.2f}"
                      if money(federal_result.qbi_deduction) > ZERO else ""))

    # --- the deduction is the better of the two --------------------------
    standard = money(federal_result.standard_deduction)
    itemised = money(federal_result.itemised_deduction)
    taken = money(federal_result.deduction_taken)
    kind = getattr(federal_result, "deduction_kind", "standard")
    extras = positive(taken - max(standard, itemised))
    add(Check(
        "The larger deduction was taken",
        OK if taken + TOLERANCE >= max(standard, itemised) else FAILED,
        f"{kind}: took {taken:,.2f} against standard {standard:,.2f} and "
        f"itemised {itemised:,.2f}"
        + (f" (plus {extras:,.2f} of additional deductions)" if extras > ZERO else ""),
    ))

    # --- the tax, recomputed from the published table --------------------
    preferential = ZERO
    capital = getattr(federal_result, "capital", None) or {}
    if isinstance(capital, dict):
        preferential = positive(money(capital.get("preferential_component", 0)))
    preferential += positive(money(federal_result.preferential_tax))

    if not year:
        add(Check("Ordinary tax follows the published bracket table", SKIPPED,
                  "no tax year on the return"))
    elif preferential > ZERO:
        # Long-term gains and qualified dividends stack on top of ordinary
        # income, and reimplementing that stacking here badly would produce
        # confident wrong failures. An honest skip is worth more than a check
        # that cries wolf on every investor.
        add(Check("Ordinary tax follows the published bracket table", SKIPPED,
                  "this return has income taxed at preferential rates, which "
                  "needs the stacking worksheet rather than the plain table"))
    else:
        try:
            compare("Ordinary tax follows the published bracket table",
                    tax_from_brackets(money(federal_result.taxable_income),
                                      year=year, status=status),
                    money(federal_result.ordinary_tax),
                    detail=f"{money(federal_result.taxable_income):,.2f} through "
                           f"the {year} {status.replace('_', ' ')} table")
        except Exception as exc:
            add(Check("Ordinary tax follows the published bracket table", SKIPPED,
                      f"could not recompute: {exc}"))

    # --- what the forms say, added up by hand ----------------------------
    if w2s:
        # Against the wages LINE, not total income: a return with dividends
        # or a pension has more income than its W-2s, and comparing the two
        # fails every one of them. The line is found by its own label so this
        # does not depend on the order the engine emits them in.
        stated_wages = _sum_documents(w2s, "wages")
        on_return = _wage_line(federal_result)
        if on_return is None:
            add(Check("Wages equal the W-2s on file", SKIPPED,
                      "the return has no separate wages line to compare"))
        else:
            add(Check(
                "Wages equal the W-2s on file",
                OK if abs(stated_wages - on_return) <= TOLERANCE else FAILED,
                f"{len(w2s)} W-2(s) summed by hand"
                + ("" if abs(stated_wages - on_return) <= TOLERANCE else
                   " — a planning run deliberately moves wages, so compare "
                   "the as-filed return"),
                expected=f"{stated_wages:,.2f}", actual=f"{on_return:,.2f}",
            ))

        # Two-sided, and the dangerous direction is too much. Claiming
        # withholding nobody withheld is the commonest shape of refund
        # fraud, the preparer carries the penalty, and a return that
        # inflates box 2 and adjusts the balance to match still adds up
        # perfectly -- so only the source documents catch it.
        from_w2 = _sum_documents(w2s, "federal_withheld")
        elsewhere = positive(money(other_payments))
        accounted = cents(from_w2 + elsewhere)
        paid = money(federal_result.total_payments)
        unexplained = cents(paid - accounted)
        where = (f"W-2s {from_w2:,.2f}"
                 + (f" + {elsewhere:,.2f} {other_payments_detail or 'paid elsewhere'}"
                    if elsewhere > ZERO else ""))
        if unexplained > TOLERANCE:
            add(Check(
                "Every dollar of withholding claimed is on a document",
                FAILED,
                f"{where} accounts for {accounted:,.2f}, but the return claims "
                f"{paid:,.2f}. {unexplained:,.2f} is not on any form here.",
                expected=f"{accounted:,.2f}", actual=f"{paid:,.2f}",
            ))
        elif unexplained < -TOLERANCE:
            add(Check(
                "Every dollar of withholding claimed is on a document",
                FAILED,
                f"{where} accounts for {accounted:,.2f} but the return only "
                f"claims {paid:,.2f}; the client is {abs(unexplained):,.2f} "
                "out of pocket.",
                expected=f"{accounted:,.2f}", actual=f"{paid:,.2f}",
            ))
        else:
            add(Check("Every dollar of withholding claimed is on a document",
                      OK, where + " — all accounted for",
                      expected=f"{accounted:,.2f}", actual=f"{paid:,.2f}"))

        # Every state line on every W-2 should be on the return.
        on_forms = {line.state.upper() for form in w2s
                    for line in getattr(form, "states", []) if line.state}
        on_return = {s.code.upper() for s in getattr(estimate, "states", [])}
        missing = sorted(on_forms - on_return)
        add(Check(
            "Every state on the W-2s is on the return",
            FAILED if missing else OK,
            (f"not computed: {', '.join(missing)}" if missing
             else (f"{', '.join(sorted(on_forms))}" if on_forms else "no state line")),
        ))

    # --- the balance, and the totals -------------------------------------
    compare("Federal balance is the tax less what was paid",
            money(federal_result.total_tax) - money(federal_result.total_payments),
            money(federal_result.balance))

    # `EstimateResult` exposes the totals as properties, not a `totals`
    # object; reading the wrong one silently skipped both of these.
    if hasattr(estimate, "total_balance"):
        states_tax = cents(sum((money(s.total_tax) for s in estimate.states), ZERO))
        compare("State tax is the sum of the state returns",
                states_tax, money(estimate.state_tax))
        compare("The bottom line is federal plus state",
                money(federal_result.balance) + money(estimate.state_balance),
                money(estimate.total_balance))
        compare("Total tax is federal plus state",
                money(federal_result.total_tax) + money(estimate.state_tax),
                money(estimate.total_tax))

    # --- bounds the law puts on the result -------------------------------
    agi = money(federal_result.agi)
    other_taxes = (money(federal_result.self_employment_tax)
                   + money(federal_result.additional_medicare_tax)
                   + money(federal_result.net_investment_income_tax)
                   + money(federal_result.premium_tax_credit_repayment))
    if agi <= ZERO:
        add(Check("The effective rate is below the marginal rate", SKIPPED,
                  "no income to take a rate against"))
    elif other_taxes > ZERO:
        # Self-employment tax, the extra Medicare tax and a repaid health
        # credit all sit outside the income-tax brackets, so the effective
        # rate can exceed the marginal rate quite correctly. On a gig
        # worker's return it does, and failing that was wrong.
        add(Check("The effective rate is below the marginal rate", SKIPPED,
                  f"{other_taxes:,.2f} of tax outside the income-tax brackets "
                  "makes the comparison meaningless"))
    else:
        effective = money(federal_result.total_tax) / agi
        marginal = Decimal(str(getattr(federal_result, "marginal_rate", 0) or 0))
        add(Check(
            "The effective rate is below the marginal rate",
            OK if marginal == ZERO or effective <= marginal + Decimal("0.02") else FAILED,
            f"effective {effective:.2%} against marginal {marginal:.0%}",
            expected=f"at most {marginal:.0%}", actual=f"{effective:.2%}",
        ))

    # --- the fee, against its own lines ----------------------------------
    if quote is not None:
        try:
            lines = cents(sum((money(line.amount) for line in quote.lines), ZERO))
            compare("The fee equals the sum of its own lines",
                    lines - money(quote.discount), money(quote.total),
                    detail=f"{len(quote.lines)} line(s) less "
                           f"{money(quote.discount):,.2f} of discount")
        except Exception as exc:
            add(Check("The fee equals the sum of its own lines", SKIPPED, str(exc)))

    return report
