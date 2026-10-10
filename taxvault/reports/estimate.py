"""The estimate, as a document somebody can hold.

Three things made this worth building as its own module rather than a
template in the API layer.

**It is a record, not a view.** What a client was told on 14 March matters
even after their situation changes in April. The document is therefore
rendered from the payload stored with the estimate, not from a fresh run, so
re-downloading it next year produces the same paper. Rows persisted before
the full payload was kept render from the summary that was kept, and the
document says so rather than quietly showing less.

**One content model, two renderers.** `build_estimate_document` decides what
the document says; `render_html` and `render_pdf` only decide how it looks.
A figure can therefore never appear in the PDF but not the printout, which is
the failure mode of keeping two templates.

**It has to be honest about what it is.** This is an estimate from a practice,
not a filed return and not an IRS document. Every page says so, because a
client who mistakes one for the other may not pay what they owe.
"""

from __future__ import annotations

import html
import os
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from taxvault.money import ZERO, money

#: What a client is told the document is, in the one place that controls it.
TITLE = "Estimated tax summary"

#: Printed on every page. A client who thinks this is their filed return is a
#: client who does not file, so this is not a footnote to be tuned away.
DISCLAIMER = (
    "This is an estimate prepared for you. It is not a filed tax return, it "
    "has not been sent to the IRS or to any state, and no payment has been "
    "made on your behalf. Figures are computed from the documents and answers "
    "on file on the date shown and will change if those change."
)


# ===========================================================================
# The content model
# ===========================================================================
@dataclass
class Row:
    """One line. `amount` is already formatted, or None for a heading row."""

    label: str
    amount: str | None = None
    form: str = ""
    note: str = ""
    kind: str = "line"  # line | subtotal | total | heading


@dataclass
class Section:
    title: str
    rows: list[Row] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    blurb: str = ""


@dataclass
class EstimateDocument:
    """Everything the document says, before anybody decides how it looks."""

    title: str = TITLE
    practice: str = ""
    practice_address: str = ""
    practice_phone: str = ""
    preparer: str = ""
    ptin: str = ""

    client_name: str = ""
    ssn_masked: str = ""
    tax_year: int = 0
    filing_status: str = ""
    resident_state: str = ""
    method: str = "regular"

    prepared_on: date | None = None
    estimate_id: int | None = None
    reference: str = ""

    headline_label: str = ""
    headline_amount: str = ""
    headline_is_refund: bool = False

    sections: list[Section] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    partial: bool = False

    def filename(self) -> str:
        """A name that sorts and says what it is without being opened."""
        who = "".join(ch for ch in self.client_name if ch.isalnum()) or "client"
        return f"{self.tax_year}-estimate-{who}.pdf"


# ===========================================================================
# Formatting
# ===========================================================================
def _money(value: Any) -> str:
    try:
        return f"${money(value):,.2f}"
    except Exception:
        return "$0.00"


def _amount(value: Any) -> Decimal:
    try:
        return money(value)
    except Exception:
        return ZERO


def _nonzero(value: Any) -> bool:
    return _amount(value) != ZERO


def _signed(value: Any) -> str:
    """A subtraction with the sign where a reader expects it.

    "$-15,750.00" reads as a typo; "−$15,750.00" reads as money coming off.
    """
    amount = _amount(value)
    return ("−" + _money(abs(amount))) if amount < ZERO else _money(amount)


def _minus(value: Any) -> str:
    """A figure being taken away. Zero is not negative, so it keeps no sign."""
    amount = _amount(value)
    return _money(ZERO) if amount == ZERO else "−" + _money(abs(amount))


#: Keyed on `taxvault.enums.FilingStatus`. The titled fallback below covers a
#: value this map has not caught up with, rather than printing a raw code.
_STATUS = {
    "single": "Single",
    "married_jointly": "Married filing jointly",
    "married_separately": "Married filing separately",
    "head_of_household": "Head of household",
    "qualifying_surviving_spouse": "Qualifying surviving spouse",
}


def _status_label(code: str) -> str:
    return _STATUS.get(code, (code or "").replace("_", " ").title())


# ===========================================================================
# Building
# ===========================================================================
def build_estimate_document(
    payload: dict[str, Any],
    *,
    client_name: str = "",
    ssn_last4: str = "",
    estimate_id: int | None = None,
    prepared_on: date | None = None,
    practice: str = "",
    practice_address: str = "",
    practice_phone: str = "",
    preparer: str = "",
    ptin: str = "",
    partial: bool = False,
) -> EstimateDocument:
    """Turn a stored estimate payload into the document's content.

    `payload` is what `/api/estimates` returned. `partial` says the payload is
    a pre-payload summary record rather than a full run, which the document
    states on its face instead of silently printing a thinner page.
    """
    federal = payload.get("federal") or {}
    totals = payload.get("totals") or {}
    states = payload.get("states") or []

    doc = EstimateDocument(
        practice=practice or os.getenv("TAXVAULT_PRACTICE_NAME", ""),
        practice_address=practice_address or os.getenv("TAXVAULT_PRACTICE_ADDRESS", ""),
        practice_phone=practice_phone or os.getenv("TAXVAULT_PRACTICE_PHONE", ""),
        preparer=preparer or os.getenv("TAXVAULT_PREPARER_NAME", ""),
        ptin=ptin or os.getenv("TAXVAULT_PREPARER_PTIN", ""),
        client_name=client_name or "Client",
        # ASCII only: reportlab's standard fonts have no bullet glyph, and a
        # mask that renders as "--6789" on paper is worse than words.
        ssn_masked=f"SSN ending {ssn_last4}" if ssn_last4 else "",
        tax_year=int(payload.get("tax_year") or 0),
        filing_status=_status_label(payload.get("filing_status", "")),
        resident_state=payload.get("resident_state") or "",
        method=payload.get("method", "regular"),
        prepared_on=prepared_on or date.today(),
        estimate_id=estimate_id,
        partial=partial,
    )
    if estimate_id:
        doc.reference = f"TV-EST-{doc.tax_year % 100:02d}-{estimate_id:06d}"

    # ------------------------------------------------------------ headline
    balance = _amount(totals.get("total_balance"))
    doc.headline_is_refund = balance < ZERO
    doc.headline_label = (
        "Estimated refund" if doc.headline_is_refund else
        "Estimated balance to pay" if balance > ZERO else
        "Estimated balance"
    )
    doc.headline_amount = _money(abs(balance))

    doc.sections = [
        section for section in (
            _income_section(federal),
            _tax_section(federal),
            _capital_section(federal.get("capital") or {}),
            _retirement_section(federal.get("retirement") or {}),
            _mortgage_section(federal.get("mortgage") or {}),
            _health_section(federal.get("premium_tax_credit") or {}),
            _state_section(states),
            _bottom_line_section(federal, totals, states),
            _planning_section(payload),
            _payment_section(payload.get("payment") or {}),
        ) if section is not None
    ]

    doc.warnings = [str(w) for w in (federal.get("warnings") or [])]
    doc.warnings += [str(w) for w in (payload.get("warnings") or [])]
    doc.notes = [str(n) for n in (federal.get("notes") or [])]
    for state in states:
        for note in state.get("notes") or []:
            doc.notes.append(f"{state.get('name', state.get('code', 'State'))}: {note}")
    return doc


#: Where the engine's 1040 line list stops being income and starts being the
#: computation. Everything from here on is rendered by the sections that
#: explain it, so the income section stops short of it.
_INCOME_ENDS_AT = "total income"


def _income_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The income part of the 1040 spine, and nothing after it.

    `federal.lines` runs from wages all the way to the balance owed. Rendering
    all of it under "Income" printed the deduction, the tax and the balance
    again under their own headings -- the same figure three times, which makes
    a client distrust every figure on the page.
    """
    out = []
    for line in lines:
        if str(line.get("label", "")).strip().lower() == _INCOME_ENDS_AT:
            break
        # Schedule D's component rows belong to the investments section. At
        # zero they are noise; non-zero, they are shown there in full.
        if not _nonzero(line.get("amount")):
            # A line at zero is a kind of income this client does not have.
            # "Wages, salaries, tips $0.00" on a self-employed return invites
            # the question "where did my wages go?" and answers nothing.
            continue
        out.append(line)
    return out


def _income_section(federal: dict[str, Any]) -> Section | None:
    lines = _income_lines(federal.get("lines") or [])
    if not lines and not _nonzero(federal.get("total_income")):
        return None
    section = Section(
        title="Income",
        blurb="Every figure here came off a form you gave us. The form it came "
              "from is named beside it so you can check it against the paper.",
    )
    for line in lines:
        section.rows.append(Row(
            label=str(line.get("label", "")),
            amount=_signed(line.get("amount")),
            form=str(line.get("form", "")),
            note=str(line.get("note", "")),
        ))
    section.rows.append(Row("Total income", _money(federal.get("total_income")),
                            form="1040 line 9", kind="subtotal"))
    if _nonzero(federal.get("adjustments")):
        section.rows.append(Row("Adjustments to income",
                                _minus(federal.get("adjustments")),
                                form="Schedule 1"))
    section.rows.append(Row("Adjusted gross income", _money(federal.get("agi")),
                            form="1040 line 11", kind="total"))
    return section


def _tax_section(federal: dict[str, Any]) -> Section:
    section = Section(title="Federal tax")
    kind = federal.get("deduction_kind", "standard")
    section.rows.append(Row(
        f"{'Itemised' if kind == 'itemised' else 'Standard'} deduction",
        _minus(federal.get("deduction_taken")),
        form="1040 line 12",
        note=("Itemising beats the standard deduction for you this year."
              if kind == "itemised" else
              f"The standard deduction ({_money(federal.get('standard_deduction'))}) "
              f"beats itemising ({_money(federal.get('itemised_deduction'))})."),
    ))
    for key, label, form in (
        ("senior_deduction", "Additional deduction, age 65 or over", "1040 line 12"),
        ("obbba_deductions", "Tips, overtime, car-loan and senior deductions", "Schedule 1-A"),
        ("qbi_deduction", "Qualified business income deduction", "1040 line 13"),
    ):
        if _nonzero(federal.get(key)):
            section.rows.append(Row(label, _minus(federal.get(key)), form=form))

    section.rows.append(Row("Taxable income", _money(federal.get("taxable_income")),
                            form="1040 line 15", kind="subtotal"))
    section.rows.append(Row("Tax on ordinary income", _money(federal.get("ordinary_tax")),
                            form="1040 line 16"))
    for key, label, form, note in (
        ("preferential_tax", "Tax on long-term gains and qualified dividends",
         "Sch D worksheet", "Taxed at 0%, 15% or 20% instead of your ordinary rate."),
        ("amt", "Alternative minimum tax", "Form 6251", ""),
        ("self_employment_tax", "Self-employment tax", "Schedule SE", ""),
        ("additional_medicare_tax", "Additional Medicare tax", "Form 8959", ""),
        ("net_investment_income_tax", "Net investment income tax", "Form 8960", ""),
        ("early_withdrawal_penalty", "Additional tax on early distributions",
         "Form 5329", "10% of the taxable part, where no exception applies."),
        ("premium_tax_credit_repayment", "Advance premium tax credit repaid",
         "Form 8962", ""),
    ):
        if _nonzero(federal.get(key)):
            section.rows.append(Row(label, _money(federal.get(key)), form=form, note=note))

    if _nonzero(federal.get("nonrefundable_credits")):
        section.rows.append(Row("Nonrefundable credits",
                                _minus(federal.get("nonrefundable_credits")),
                                form="Schedule 3"))
    if _nonzero(federal.get("refundable_credits")):
        section.rows.append(Row("Refundable credits",
                                _minus(federal.get("refundable_credits")),
                                form="1040 line 32"))
    section.rows.append(Row("Total federal tax", _money(federal.get("total_tax")),
                            form="1040 line 24", kind="total"))
    rate = federal.get("effective_rate")
    marginal = federal.get("marginal_rate")
    if rate is not None:
        section.notes.append(
            f"Your effective rate is {float(rate) * 100:.1f}% of adjusted gross "
            f"income. Your marginal rate — what the next dollar costs — is "
            f"{float(marginal or 0) * 100:.0f}%. The two are different and the "
            "marginal one is what planning moves work against."
        )
    return section


def _capital_section(capital: dict[str, Any]) -> Section | None:
    if not capital or not any(_nonzero(capital.get(k)) for k in (
            "short_term_net", "long_term_net", "loss_deduction", "carryforward_short",
            "carryforward_long", "wash_sale_disallowed")):
        return None
    section = Section(
        title="Investments (Schedule D)",
        blurb="Short-term and long-term are netted separately. Whichever "
              "survives keeps its own character, which is the part spreadsheets "
              "get wrong.",
    )
    section.rows.append(Row("Net short-term gain or loss",
                            _signed(capital.get("short_term_net")), form="Sch D line 7",
                            note="Taxed at your ordinary rate."))
    section.rows.append(Row("Net long-term gain or loss",
                            _signed(capital.get("long_term_net")), form="Sch D line 15",
                            note="Taxed at 0%, 15% or 20%."))
    if _nonzero(capital.get("loss_deduction")):
        section.rows.append(Row("Loss deducted against other income",
                                _minus(capital.get("loss_deduction")),
                                form="1040 line 7",
                                note="Capped at $3,000 a year ($1,500 if married "
                                     "filing separately)."))
    for key, label in (("carryforward_short", "Short-term loss carried to next year"),
                       ("carryforward_long", "Long-term loss carried to next year")):
        if _nonzero(capital.get(key)):
            section.rows.append(Row(label, _signed(capital.get(key)),
                                    note="Carried forward keeping its character. "
                                         "It does not expire."))
    if _nonzero(capital.get("wash_sale_disallowed")):
        section.rows.append(Row("Loss disallowed by the wash-sale rule",
                                _money(capital.get("wash_sale_disallowed")),
                                note="Repurchased within 30 days, so the loss is "
                                     "added to the new lot's basis, not lost."))
    section.notes += [str(n) for n in (capital.get("notes") or [])]
    return section


def _retirement_section(retirement: dict[str, Any]) -> Section | None:
    if not retirement or not _nonzero(retirement.get("gross")):
        return None
    section = Section(
        title="Retirement distributions (Form 1099-R)",
        blurb="What came out of a retirement account, and how much of it is "
              "taxable.",
    )
    section.rows.append(Row("Gross distributions", _money(retirement.get("gross")),
                            form="1099-R box 1"))
    if _nonzero(retirement.get("rolled_over")):
        section.rows.append(Row("Rolled over, not taxable",
                                _minus(retirement.get("rolled_over")),
                                form="1040 line 5a"))
    section.rows.append(Row("Taxable amount", _money(retirement.get("taxable")),
                            form="1040 line 5b", kind="subtotal"))
    if _nonzero(retirement.get("penalty")):
        section.rows.append(Row("Additional 10% tax", _money(retirement.get("penalty")),
                                form="Form 5329"))
    for key, label in (("federal_withheld", "Federal tax already withheld"),
                       ("state_withheld", "State tax already withheld")):
        if _nonzero(retirement.get(key)):
            section.rows.append(Row(label, _money(retirement.get(key)),
                                    form="1099-R box 4/14"))
    section.notes += [str(n) for n in (retirement.get("notes") or [])]
    section.notes += [str(w) for w in (retirement.get("warnings") or [])]
    return section


def _mortgage_section(mortgage: dict[str, Any]) -> Section | None:
    if not mortgage or not _nonzero(mortgage.get("total_interest")):
        return None
    section = Section(
        title="Home loan (Form 1098)",
        blurb="Interest above the debt ceiling is prorated, not disallowed. "
              "The deductible share is the ratio of allowed debt to your balance.",
    )
    section.rows.append(Row("Interest paid", _money(mortgage.get("total_interest")),
                            form="1098 box 1"))
    section.rows.append(Row("Qualifying debt", _money(mortgage.get("qualifying_debt"))))
    section.rows.append(Row("Debt inside the ceiling", _money(mortgage.get("allowed_debt"))))
    section.rows.append(Row("Deductible interest",
                            _money(mortgage.get("deductible_interest")),
                            form="Schedule A line 8a", kind="subtotal"))
    for key, label, form in (
        ("points_deductible", "Points deductible this year", "Schedule A line 8c"),
        ("mortgage_insurance", "Mortgage insurance premiums", "Schedule A line 8d"),
    ):
        if _nonzero(mortgage.get(key)):
            section.rows.append(Row(label, _money(mortgage.get(key)), form=form))
    section.notes += [str(n) for n in (mortgage.get("notes") or [])]
    return section


def _health_section(ptc: dict[str, Any]) -> Section | None:
    if not ptc or not any(_nonzero(ptc.get(k)) for k in
                          ("advance_paid", "allowed_credit", "repayment", "extra_credit")):
        return None
    section = Section(
        title="Marketplace health coverage (Form 8962)",
        blurb="The credit is reconciled against what was paid to your insurer "
              "in advance during the year.",
    )
    if ptc.get("income_as_pct_of_fpl") is not None:
        section.rows.append(Row("Household income as a share of the poverty line",
                                f"{ptc.get('income_as_pct_of_fpl')}%"))
    section.rows.append(Row("Advance credit already paid to your insurer",
                            _money(ptc.get("advance_paid")), form="1095-A column C"))
    section.rows.append(Row("Credit you are actually allowed",
                            _money(ptc.get("allowed_credit")), form="8962 line 24"))
    if _nonzero(ptc.get("repayment")):
        section.rows.append(Row("Advance credit to repay", _money(ptc.get("repayment")),
                                form="8962 line 29", kind="subtotal"))
    if _nonzero(ptc.get("extra_credit")):
        section.rows.append(Row("Additional credit owed to you",
                                _money(ptc.get("extra_credit")),
                                form="8962 line 26", kind="subtotal"))
    if ptc.get("over_cliff"):
        section.notes.append(
            "Household income is over 400% of the federal poverty line, so the "
            "entire advance credit is repayable with no cap. This is the single "
            "most expensive line on this estimate and it is worth asking whether "
            "a deductible contribution can bring income back under the limit."
        )
    section.notes += [str(n) for n in (ptc.get("notes") or [])]
    return section


def _is_untaxed(state: dict[str, Any]) -> bool:
    """A state that took nothing and is owed nothing.

    `kind == "none"` alone is not enough: Washington levies no tax on wages
    but does charge an excise on large long-term gains, and a filer who owes
    that needs the figures, not a reassuring sentence.
    """
    return (state.get("kind") == "none"
            and not _nonzero(state.get("total_tax"))
            and not _nonzero(state.get("withheld"))
            and not _nonzero(state.get("balance")))


def _state_section(states: list[dict[str, Any]]) -> Section | None:
    if not states:
        return None
    # Texas, Florida, Nevada and the rest levy nothing on wages. A table of
    # four zeros under their name tells a client nothing; the one useful
    # sentence is that there is no state income tax at all.
    if all(_is_untaxed(state) for state in states):
        section = Section(title="State tax")
        for state in states:
            name = state.get("name") or state.get("code") or "State"
            # The state engine reports its commentary as `notes`; the single
            # `note` is the config field. Prefer whichever is there.
            said = state.get("notes") or []
            note = (said[0] if said else state.get("note") or "").strip()
            section.rows.append(Row(
                f"{name} — no state income tax", _money(0),
                note=note or "Nothing is owed to this state on this income.",
            ))
        section.notes.append(
            "No state return is needed for income tax. A state with no income "
            "tax still raises money other ways -- sales tax and property tax "
            "among them -- and the sales tax may be deductible on your federal "
            "return, which is why it is asked about."
        )
        return section

    section = Section(
        title="State tax",
        blurb="State figures are modelled at statewide level. Local taxes, where "
              "a state has them, are shown separately.",
    )
    for state in states:
        name = state.get("name") or state.get("code") or "State"
        section.rows.append(Row(name, kind="heading"))
        lines = state.get("lines") or []
        for line in lines:
            section.rows.append(Row(f"   {line.get('label', '')}",
                                    _signed(line.get("amount")),
                                    note=str(line.get("note", ""))))
        if not lines:
            # Only when the engine gave no line list of its own. Where it did,
            # it already carries these, and adding them listed the exemption
            # credit twice on the same page.
            for key, label, negate in (("local_tax", "   Local income tax", False),
                                       ("surtax", "   Surtax", False),
                                       ("credits", "   State credits", True)):
                if _nonzero(state.get(key)):
                    shown = _money(state.get(key))
                    section.rows.append(Row(label, ("−" + shown) if negate else shown))
        section.rows.append(Row(f"   {name} tax", _money(state.get("total_tax")),
                                kind="subtotal"))
        section.rows.append(Row("   Already withheld", _minus(state.get("withheld"))))
        balance = _amount(state.get("balance"))
        section.rows.append(Row(
            "   Refund" if balance < ZERO else "   To pay",
            _money(abs(balance)), kind="total"))
    return section


def _bottom_line_section(federal: dict[str, Any], totals: dict[str, Any],
                         states: list[dict[str, Any]]) -> Section:
    taxed_anywhere = bool(states) and not all(_is_untaxed(state) for state in states)
    section = Section(
        title="Where this leaves you",
        blurb=("Federal and state are separate debts to separate governments. "
               "They are shown together here only so you can see the whole year."
               if taxed_anywhere else
               "There is no state income tax where you live, so this is the "
               "whole year's income tax."),
    )
    section.rows.append(Row("Total federal tax", _money(totals.get("federal_tax"))))
    section.rows.append(Row("Federal tax already paid",
                            _minus(federal.get("total_payments")),
                            note="Withholding from your W-2s plus anything you paid "
                                 "in during the year."))
    fed_balance = _amount(totals.get("federal_balance"))
    section.rows.append(Row(
        "Federal refund" if fed_balance < ZERO else "Federal balance to pay",
        _money(abs(fed_balance)), kind="subtotal"))
    if states and not all(_is_untaxed(state) for state in states):
        state_balance = _amount(totals.get("state_balance"))
        section.rows.append(Row("Total state tax", _money(totals.get("state_tax"))))
        section.rows.append(Row(
            "State refund" if state_balance < ZERO else "State balance to pay",
            _money(abs(state_balance)), kind="subtotal"))
    total = _amount(totals.get("total_balance"))
    section.rows.append(Row(
        "Your refund" if total < ZERO else "Total to pay",
        _money(abs(total)), kind="total"))
    return section


def _planning_section(payload: dict[str, Any]) -> Section | None:
    strategies = payload.get("strategies") or []
    applied = payload.get("applied") or []
    if not strategies:
        return None
    section = Section(
        title="What could still change this",
        blurb="Moves that are open to you, with what each one saves. A move "
              "marked as applied is already in the figures above.",
    )
    for item in strategies:
        key = item.get("id") or item.get("key") or ""
        # Federal and state savings are reported separately by the engine. A
        # client cares what the move is worth in total, so they are added --
        # a state-only move showed as $0.00 when only the federal field was read.
        saving = _amount(item.get("federal_saving")) + _amount(item.get("state_saving"))
        bits = [str(item.get("summary") or item.get("how_it_works") or "")]
        if key in applied or item.get("applied"):
            bits.insert(0, "Already included in the figures above.")
        if _nonzero(item.get("cash_required")):
            bits.append(f"Needs {_money(item.get('cash_required'))} of cash.")
        if item.get("closes_on"):
            bits.append(f"Closes {item['closes_on']}.")
        if item.get("still_available") is False:
            bits.append("The window for this year has closed.")
        section.rows.append(Row(
            str(item.get("label") or item.get("name") or key),
            _money(saving),
            note=" ".join(b for b in bits if b),
        ))
    saved = payload.get("saving_against_baseline")
    if _nonzero(saved):
        section.rows.append(Row("Saved against filing with no moves at all",
                                _money(saved), kind="total"))
    return section


def _payment_section(payment: dict[str, Any]) -> Section | None:
    options = payment.get("options") or []
    if payment.get("direction") != "balance_due" or not options:
        return None
    section = Section(
        title="How to pay",
        blurb="Paid to the government, not to this practice. Every route below "
              "is one you set up yourself, and the cost of each is shown in full.",
    )
    best = [o for o in options if o.get("recommended")] or options[:3]
    for option in best:
        bits = []
        if _nonzero(option.get("setup_fee")):
            bits.append(f"set-up {_money(option.get('setup_fee'))}")
        if _nonzero(option.get("processing_fee")):
            bits.append(f"processing {_money(option.get('processing_fee'))}")
        if _nonzero(option.get("penalty")):
            bits.append(f"penalty {_money(option.get('penalty'))}")
        if _nonzero(option.get("interest")):
            bits.append(f"interest {_money(option.get('interest'))}")
        note = ", ".join(bits)
        if option.get("final_due_on"):
            note = (note + f" · due {option['final_due_on']}").strip(" ·")
        section.rows.append(Row(
            str(option.get("label", option.get("method", ""))),
            _money(option.get("total_cost")), note=note))
    section.notes.append(
        "An extension gives you six more months to file. It does not give you "
        "six more months to pay: interest and the failure-to-pay penalty run "
        "from the April due date either way."
    )
    return section


# ===========================================================================
# HTML
# ===========================================================================
def render_html(doc: EstimateDocument) -> str:
    """A self-contained page. Printing it from a browser gives a usable PDF,
    which is what a practice without reportlab installed will do."""
    e = html.escape

    def rows(section: Section) -> str:
        out = []
        for row in section.rows:
            if row.kind == "heading":
                out.append(f'<tr class="heading"><th colspan="2">{e(row.label)}</th></tr>')
                continue
            note = f'<span class="note">{e(row.note)}</span>' if row.note else ""
            form = f'<span class="form">{e(row.form)}</span>' if row.form else ""
            out.append(
                f'<tr class="{e(row.kind)}">'
                f"<td>{e(row.label)}{form}{note}</td>"
                f'<td class="num">{e(row.amount or "")}</td></tr>'
            )
        return "".join(out)

    sections = "".join(
        f'<section><h2>{e(s.title)}</h2>'
        + (f'<p class="blurb">{e(s.blurb)}</p>' if s.blurb else "")
        + f"<table>{rows(s)}</table>"
        + "".join(f'<p class="snote">{e(n)}</p>' for n in s.notes)
        + "</section>"
        for s in doc.sections
    )

    ident = " · ".join(filter(None, [
        e(doc.filing_status),
        f"{e(doc.resident_state)} resident" if doc.resident_state else "",
        e(doc.ssn_masked),
    ]))
    practice_bits = " · ".join(filter(None, [
        e(doc.practice_address), e(doc.practice_phone),
        f"Preparer {e(doc.preparer)}" if doc.preparer else "",
        f"PTIN {e(doc.ptin)}" if doc.ptin else "",
    ]))
    prepared = doc.prepared_on.strftime("%d %B %Y") if doc.prepared_on else ""

    warnings = "".join(f"<li>{e(w)}</li>" for w in doc.warnings)
    notes = "".join(f"<li>{e(n)}</li>" for n in doc.notes)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(doc.title)} — {e(str(doc.tax_year))} — {e(doc.client_name)}</title>
<style>
  :root {{ --ink:#12161c; --muted:#5b6672; --line:#d9dee5; --navy:#18305F;
           --good:#107A4B; --warn:#b45309; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; padding:32px 24px; color:var(--ink); background:#fff;
         font:14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         max-width:780px; margin-inline:auto; }}
  header {{ border-bottom:2px solid var(--navy); padding-bottom:14px; margin-bottom:20px; }}
  .practice {{ font-size:17px; font-weight:700; color:var(--navy); }}
  .practice-meta, .meta {{ font-size:12px; color:var(--muted); margin-top:3px; }}
  h1 {{ font-size:20px; margin:14px 0 2px; }}
  .headline {{ border:1px solid var(--line); border-left:4px solid
               {'var(--good)' if doc.headline_is_refund else 'var(--warn)'};
               border-radius:8px; padding:14px 16px; margin:18px 0 24px; }}
  .headline .label {{ font-size:12px; text-transform:uppercase; letter-spacing:.06em;
                      color:var(--muted); }}
  .headline .amount {{ font:700 30px/1.1 ui-monospace, Menlo, Consolas, monospace;
                       color:{'var(--good)' if doc.headline_is_refund else 'var(--warn)'};
                       margin-top:4px; }}
  section {{ margin-bottom:26px; break-inside:avoid; }}
  h2 {{ font-size:14px; text-transform:uppercase; letter-spacing:.06em;
        color:var(--navy); border-bottom:1px solid var(--line);
        padding-bottom:5px; margin:0 0 8px; }}
  .blurb {{ font-size:12.5px; color:var(--muted); margin:0 0 10px; }}
  table {{ width:100%; border-collapse:collapse; }}
  td, th {{ padding:6px 0; vertical-align:top; text-align:left; }}
  td.num {{ text-align:right; white-space:nowrap;
            font-family:ui-monospace, Menlo, Consolas, monospace; }}
  tr.subtotal td {{ border-top:1px solid var(--line); font-weight:600; }}
  tr.total td {{ border-top:2px solid var(--ink); font-weight:700; }}
  tr.heading th {{ padding-top:12px; font-size:13px; }}
  .form {{ display:block; font-size:11px; color:var(--muted);
           font-family:ui-monospace, Menlo, Consolas, monospace; }}
  .note {{ display:block; font-size:12px; color:var(--muted); }}
  .snote {{ font-size:12px; color:var(--muted); margin:8px 0 0; }}
  .disclaimer {{ border:1px solid var(--line); border-radius:8px; padding:12px 14px;
                 font-size:12px; color:var(--muted); margin-top:26px; }}
  ul {{ margin:6px 0 0; padding-left:18px; font-size:12.5px; color:var(--muted); }}
  .warn {{ color:var(--warn); }}
  @media print {{ body {{ padding:0; }} @page {{ margin:18mm; }} }}
</style></head><body>
<header>
  <div class="practice">{e(doc.practice or 'Tax estimate')}</div>
  {f'<div class="practice-meta">{practice_bits}</div>' if practice_bits else ''}
  <h1>{e(doc.title)} — tax year {e(str(doc.tax_year))}</h1>
  <div class="meta">Prepared for <strong>{e(doc.client_name)}</strong>{f' · {ident}' if ident else ''}</div>
  <div class="meta">Prepared {e(prepared)}{f' · Reference {e(doc.reference)}' if doc.reference else ''}
    {' · planning scenario' if doc.method == 'planning' else ''}</div>
</header>

<div class="headline">
  <div class="label">{e(doc.headline_label)}</div>
  <div class="amount">{e(doc.headline_amount)}</div>
</div>

{'<p class="snote warn">This document was produced from a summary record kept before full estimates were stored, so it shows fewer lines than a current estimate. Re-run the estimate for the full detail.</p>' if doc.partial else ''}

{sections}

{f'<section><h2>Please check these</h2><ul class="warn">{warnings}</ul></section>' if warnings else ''}
{f'<section><h2>Notes</h2><ul>{notes}</ul></section>' if notes else ''}

<div class="disclaimer">{e(DISCLAIMER)}</div>
</body></html>"""


# ===========================================================================
# PDF
# ===========================================================================
class PdfUnavailable(RuntimeError):
    """reportlab is not installed, so no PDF can be drawn."""


def render_pdf(doc: EstimateDocument) -> bytes:
    """The same content as `render_html`, drawn for paper.

    Raises `PdfUnavailable` rather than returning something else, so a caller
    can fall back to HTML deliberately instead of handing a client a file that
    is not the format they asked for.
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_RIGHT
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            KeepTogether,
            PageBreak,  # noqa: F401 - kept for callers that extend this
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError as exc:  # pragma: no cover - exercised by the API fallback
        raise PdfUnavailable(
            "Drawing a PDF needs reportlab: pip install reportlab"
        ) from exc

    import io

    NAVY = colors.HexColor("#18305F")
    MUTED = colors.HexColor("#5b6672")
    LINE = colors.HexColor("#d9dee5")
    GOOD = colors.HexColor("#107A4B")
    WARN = colors.HexColor("#b45309")

    base = getSampleStyleSheet()
    st = {
        "practice": ParagraphStyle("practice", parent=base["Normal"], fontSize=13,
                                   leading=16, textColor=NAVY,
                                   fontName="Helvetica-Bold"),
        "meta": ParagraphStyle("meta", parent=base["Normal"], fontSize=8,
                               leading=11, textColor=MUTED),
        "h1": ParagraphStyle("h1", parent=base["Normal"], fontSize=15, leading=19,
                             spaceBefore=8, fontName="Helvetica-Bold"),
        "h2": ParagraphStyle("h2", parent=base["Normal"], fontSize=9.5, leading=13,
                             textColor=NAVY, fontName="Helvetica-Bold",
                             spaceBefore=14, spaceAfter=3),
        "blurb": ParagraphStyle("blurb", parent=base["Normal"], fontSize=8,
                                leading=11, textColor=MUTED, spaceAfter=5),
        "cell": ParagraphStyle("cell", parent=base["Normal"], fontSize=9, leading=12),
        "cellb": ParagraphStyle("cellb", parent=base["Normal"], fontSize=9, leading=12,
                                fontName="Helvetica-Bold"),
        "sub": ParagraphStyle("sub", parent=base["Normal"], fontSize=7.5, leading=10,
                              textColor=MUTED),
        "num": ParagraphStyle("num", parent=base["Normal"], fontSize=9, leading=12,
                              alignment=TA_RIGHT, fontName="Courier"),
        "numb": ParagraphStyle("numb", parent=base["Normal"], fontSize=9, leading=12,
                               alignment=TA_RIGHT, fontName="Courier-Bold"),
        "note": ParagraphStyle("note", parent=base["Normal"], fontSize=8, leading=11,
                               textColor=MUTED, spaceBefore=4),
        "big": ParagraphStyle("big", parent=base["Normal"], fontSize=24, leading=28,
                              fontName="Courier-Bold",
                              textColor=GOOD if doc.headline_is_refund else WARN),
    }

    def esc(text: str) -> str:
        return html.escape(str(text or ""))

    story: list[Any] = []
    story.append(Paragraph(esc(doc.practice or "Tax estimate"), st["practice"]))
    practice_bits = " · ".join(filter(None, [
        doc.practice_address, doc.practice_phone,
        f"Preparer {doc.preparer}" if doc.preparer else "",
        f"PTIN {doc.ptin}" if doc.ptin else "",
    ]))
    if practice_bits:
        story.append(Paragraph(esc(practice_bits), st["meta"]))
    story.append(Paragraph(f"{esc(doc.title)} — tax year {doc.tax_year}", st["h1"]))

    ident = " · ".join(filter(None, [
        doc.filing_status,
        f"{doc.resident_state} resident" if doc.resident_state else "",
        doc.ssn_masked,
    ]))
    story.append(Paragraph(
        f"Prepared for <b>{esc(doc.client_name)}</b>" + (f" · {esc(ident)}" if ident else ""),
        st["meta"]))
    prepared = doc.prepared_on.strftime("%d %B %Y") if doc.prepared_on else ""
    tail = f" · Reference {esc(doc.reference)}" if doc.reference else ""
    tail += " · planning scenario" if doc.method == "planning" else ""
    story.append(Paragraph(f"Prepared {esc(prepared)}{tail}", st["meta"]))
    story.append(Spacer(1, 14))

    headline = Table(
        [[Paragraph(esc(doc.headline_label.upper()), st["meta"])],
         [Paragraph(esc(doc.headline_amount), st["big"])]],
        colWidths=[6.6 * inch])
    headline.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.6, LINE),
        ("LINEBEFORE", (0, 0), (0, -1), 3, GOOD if doc.headline_is_refund else WARN),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(headline)

    if doc.partial:
        story.append(Spacer(1, 8))
        story.append(Paragraph(
            "This document was produced from a summary record kept before full "
            "estimates were stored, so it shows fewer lines than a current "
            "estimate. Re-run the estimate for the full detail.", st["note"]))

    for section in doc.sections:
        block: list[Any] = [Paragraph(esc(section.title.upper()), st["h2"])]
        if section.blurb:
            block.append(Paragraph(esc(section.blurb), st["blurb"]))

        data, styles = [], [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
        for index, row in enumerate(section.rows):
            if row.kind == "heading":
                data.append([Paragraph(esc(row.label), st["cellb"]), ""])
                styles.append(("TOPPADDING", (0, index), (-1, index), 9))
                continue
            label = esc(row.label)
            extra = []
            if row.form:
                extra.append(f'<font size="7" color="#5b6672">{esc(row.form)}</font>')
            if row.note:
                extra.append(f'<font size="7.5" color="#5b6672">{esc(row.note)}</font>')
            if extra:
                label += "<br/>" + "<br/>".join(extra)
            bold = row.kind in ("subtotal", "total")
            data.append([
                Paragraph(label, st["cellb"] if bold else st["cell"]),
                Paragraph(esc(row.amount or ""), st["numb"] if bold else st["num"]),
            ])
            if row.kind == "subtotal":
                styles.append(("LINEABOVE", (0, index), (-1, index), 0.5, LINE))
            elif row.kind == "total":
                styles.append(("LINEABOVE", (0, index), (-1, index), 1.1, colors.black))

        if data:
            table = Table(data, colWidths=[4.6 * inch, 2.0 * inch])
            table.setStyle(TableStyle(styles))
            block.append(table)
        for note in section.notes:
            block.append(Paragraph(esc(note), st["note"]))
        # Short sections stay whole on one page; a long one is allowed to break
        # rather than leaving half a page empty. KeepTogether takes a list;
        # the story itself takes flowables, so the long case extends.
        if len(section.rows) <= 12:
            story.append(KeepTogether(block))
        else:
            story.extend(block)

    if doc.warnings:
        story.append(Paragraph("PLEASE CHECK THESE", st["h2"]))
        for warning in doc.warnings:
            story.append(Paragraph(f"• {esc(warning)}", st["note"]))
    if doc.notes:
        story.append(Paragraph("NOTES", st["h2"]))
        for note in doc.notes:
            story.append(Paragraph(f"• {esc(note)}", st["note"]))

    def furniture(canvas, document) -> None:
        """The disclaimer and the page number, on every page without exception."""
        canvas.saveState()
        canvas.setFont("Helvetica", 6.6)
        canvas.setFillColor(MUTED)
        width, _ = letter
        text = canvas.beginText(0.9 * inch, 0.62 * inch)
        # Wrapped by hand: this runs outside the frame, so platypus cannot do it.
        words, line = DISCLAIMER.split(), ""
        for word in words:
            if len(line) + len(word) + 1 > 150:
                text.textLine(line)
                line = word
            else:
                line = f"{line} {word}".strip()
        text.textLine(line)
        canvas.drawText(text)
        canvas.setFont("Helvetica", 7.5)
        canvas.drawRightString(width - 0.9 * inch, 0.62 * inch,
                               f"Page {canvas.getPageNumber()}")
        canvas.restoreState()

    buffer = io.BytesIO()
    template = SimpleDocTemplate(
        buffer, pagesize=letter,
        leftMargin=0.9 * inch, rightMargin=0.9 * inch,
        topMargin=0.8 * inch, bottomMargin=1.1 * inch,
        title=f"{doc.title} {doc.tax_year} - {doc.client_name}",
        author=doc.practice or "TaxVault", subject=f"Tax year {doc.tax_year} estimate",
    )
    template.build(story, onFirstPage=furniture, onLaterPages=furniture)
    return buffer.getvalue()
