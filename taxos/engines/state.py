"""State income tax.

Three shapes of jurisdiction, and the client screen shows which one they are in:

* **no tax** -- nine states. The answer is zero, but not *nothing*: Washington
  still taxes large long-term gains through an excise, and a no-tax state does
  not excuse a non-resident return where the client actually worked.
* **flat** -- one rate, but rarely one rule. Colorado starts from federal
  taxable income, Mississippi exempts the first $10,000, Utah replaces the
  deduction with a phasing credit, Massachusetts adds a millionaire's surtax.
* **graduated** -- a rate schedule, doubled for joint filers in most states,
  held the same in a handful (the marriage penalty), or given its own table.

The starting figure is Box 16 where the W-2 reports it, not federal AGI. In New
Jersey and Pennsylvania those differ by the whole 401(k) deferral, and using
AGI would understate the state's tax by the deferral times the rate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from taxos.config import states as state_params
from taxos.money import ZERO, cents, money, pct, phase_out, positive, tax_on


@dataclass
class StateResult:
    code: str
    name: str
    kind: str                       # none | flat | graduated
    taxable_income: Decimal = ZERO
    tax: Decimal = ZERO
    local_tax: Decimal = ZERO
    surtax: Decimal = ZERO
    credits: Decimal = ZERO
    withheld: Decimal = ZERO
    balance: Decimal = ZERO         # + owed / - refund
    effective_rate: Decimal = ZERO
    is_resident: bool = True
    notes: list[str] = field(default_factory=list)
    lines: list[dict[str, str]] = field(default_factory=list)

    @property
    def refund(self) -> Decimal:
        return positive(-self.balance)

    @property
    def owed(self) -> Decimal:
        return positive(self.balance)

    @property
    def total_tax(self) -> Decimal:
        return cents(self.tax + self.local_tax + self.surtax - self.credits)

    def line(self, label: str, amount: Decimal, note: str = "") -> None:
        self.lines.append({"label": label, "amount": str(cents(amount)), "note": note})

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "name": self.name, "kind": self.kind,
            "taxable_income": str(self.taxable_income), "tax": str(self.tax),
            "local_tax": str(self.local_tax), "surtax": str(self.surtax),
            "credits": str(self.credits), "total_tax": str(self.total_tax),
            "withheld": str(self.withheld), "balance": str(self.balance),
            "refund": str(self.refund), "owed": str(self.owed),
            "effective_rate": str(self.effective_rate),
            "is_resident": self.is_resident, "notes": self.notes, "lines": self.lines,
        }


def _deduction_for(state: dict[str, Any], status: str, dependents: int) -> tuple[Decimal, list[str]]:
    """Standard deduction plus whatever exemptions the state grants."""
    notes: list[str] = []
    joint = status in ("married_jointly", "qualifying_surviving_spouse")

    table = state.get("standard_deduction") or {}
    key = status if status in table else ("married_jointly" if joint else "single")
    deduction = money(table.get(key, 0)) if table else ZERO

    exemption = state.get("personal_exemption")
    if isinstance(exemption, dict):
        deduction += money(exemption.get("married_jointly" if joint else "single", 0))
    elif exemption:
        deduction += money(exemption) * (2 if joint else 1)

    dependent = state.get("dependent_exemption")
    if dependent and dependents:
        deduction += money(dependent) * dependents

    if not table and not exemption:
        notes.append(f"{state['code']} gives no standard deduction; tax starts at the first dollar.")
    return cents(deduction), notes


def compute_state(
    code: str,
    *,
    state_income: Decimal | float | str,
    withheld: Decimal | float | str = 0,
    filing_status: str = "single",
    dependents: int = 0,
    federal_taxable_income: Decimal | float | str = 0,
    federal_deduction: Decimal | float | str = 0,
    long_term_gains: Decimal | float | str = 0,
    is_resident: bool = True,
    include_local: bool = True,
    year: int | None = None,
) -> StateResult:
    params = state_params(year)
    state = params.get(code)
    result = StateResult(
        code=state["code"], name=state["name"], kind=state["type"],
        withheld=cents(withheld), is_resident=is_resident,
    )
    income = money(state_income)

    # --- no-tax jurisdictions ---------------------------------------------
    if state["type"] == "none":
        result.notes.append(state.get("note", f"{state['name']} has no individual income tax."))
        excise = state.get("capital_gains_excise")
        if excise and money(long_term_gains) > ZERO:
            allowance = money(excise["standard_deduction"])
            base = positive(money(long_term_gains) - allowance)
            if base > ZERO:
                tax = cents(base * pct(excise["rate"]))
                result.tax = tax
                result.taxable_income = base
                result.line("Long-term capital gains subject to excise", base)
                result.line("Capital gains excise tax", tax)
                result.notes.append(
                    f"{state['name']} levies a {pct(excise['rate']):.0%} excise on long-term gains "
                    f"above {allowance:,.0f}. Wages remain untaxed."
                )
        result.balance = cents(result.total_tax - result.withheld)
        if result.withheld > ZERO and state["type"] == "none":
            result.notes.append(
                f"{result.withheld:,.0f} was withheld for {state['code']}, which levies no "
                "income tax. File a return to recover it."
            )
        return result

    # --- the starting figure ----------------------------------------------
    if state.get("starts_from") == "federal_taxable_income":
        base = positive(money(federal_taxable_income))
        result.line("Federal taxable income (state starting point)", base)
        deduction, notes = ZERO, [
            f"{state['name']} starts from federal taxable income, so the federal "
            "standard deduction already applies."
        ]
    else:
        base = positive(income)
        result.line("State wages / income", base)
        deduction, notes = _deduction_for(state, filing_status, dependents)
        if deduction > ZERO:
            result.line("State deduction and exemptions", -deduction)
    result.notes.extend(notes)

    taxable = positive(base - deduction)

    # Mississippi taxes only what is above its exempt slice.
    exempt_first = state.get("exempt_first")
    if exempt_first:
        joint = filing_status in ("married_jointly", "qualifying_surviving_spouse")
        slice_amount = money(exempt_first.get("married_jointly" if joint else "single", 0))
        taxable = positive(taxable - slice_amount)
        result.line(f"Less {state['code']} exempt amount", -slice_amount)

    result.taxable_income = taxable

    # --- the tax -----------------------------------------------------------
    if state["type"] == "flat":
        rate = pct(state["rate"])
        result.tax = cents(taxable * rate)
        result.line(f"{state['code']} tax at {rate:.2%}", result.tax)
    else:
        schedule = params.brackets(state["code"], filing_status)
        result.tax = tax_on(taxable, schedule)
        top = schedule[-1].rate
        result.line(f"{state['code']} graduated tax (top band {top:.2%})", result.tax)

    # --- surtax (MA millionaire's, CA mental health) ----------------------
    surtax = state.get("surtax")
    if surtax:
        over = positive(taxable - money(surtax["over"]))
        if over > ZERO:
            result.surtax = cents(over * pct(surtax["rate"]))
            result.line(surtax.get("label", "Surtax"), result.surtax)
            result.notes.append(
                f"{surtax.get('label', 'A surtax')} of {pct(surtax['rate']):.0%} applies to the "
                f"{over:,.0f} above {money(surtax['over']):,.0f}."
            )

    # --- credits that replace a deduction ---------------------------------
    taxpayer_credit = state.get("taxpayer_credit")
    if taxpayer_credit:
        joint = filing_status in ("married_jointly", "qualifying_surviving_spouse")
        start = money((taxpayer_credit.get("phase_out_start") or {}).get(
            "married_jointly" if joint else "single", 0))
        # Utah's credit is 6% of the FEDERAL deduction (the state grants none of
        # its own), phasing out at 1.3% of income above the threshold. Basing it
        # on the state deduction would make it zero and overstate the tax.
        credit_base = money(federal_deduction) or deduction
        gross_credit = credit_base * pct(taxpayer_credit["rate"])
        result.credits += phase_out(gross_credit, base, start, rate="0.013", step=1)
        if result.credits > ZERO:
            result.line("Taxpayer credit", -result.credits)

    exemption_credit = state.get("exemption_credit")
    if exemption_credit:
        joint = filing_status in ("married_jointly", "qualifying_surviving_spouse")
        credit = money(exemption_credit.get("married_jointly" if joint else "single", 0))
        credit += money(exemption_credit.get("dependent", 0)) * dependents
        result.credits += credit
        result.line("Exemption credit", -credit)

    result.credits = min(result.credits, result.tax + result.surtax)

    # --- local income tax ---------------------------------------------------
    local_rate = state.get("local_rate_typical")
    if include_local and local_rate and is_resident:
        result.local_tax = cents(taxable * pct(local_rate))
        result.line(f"Local income tax (typical {pct(local_rate):.2%})", result.local_tax)
        result.notes.append(
            state.get("local_note", "Local income taxes apply in this state.")
            + " This uses a statewide typical rate; the exact rate depends on the locality."
        )

    if state.get("note"):
        result.notes.append(state["note"])

    result.balance = cents(result.total_tax - result.withheld)
    if base > ZERO:
        result.effective_rate = (result.total_tax / base).quantize(Decimal("0.0001"))
    return result


def compute_states(
    wage_lines: list[dict[str, Any]],
    *,
    resident_state: str,
    filing_status: str = "single",
    dependents: int = 0,
    federal_taxable_income: Decimal | float | str = 0,
    federal_deduction: Decimal | float | str = 0,
    resident_income: Decimal | float | str | None = None,
    long_term_gains: Decimal | float | str = 0,
    year: int | None = None,
) -> list[StateResult]:
    """Every state return this client owes, resident first.

    `wage_lines` is one entry per W-2 state line: `{state, wages, withheld}`.
    A client who lives in one state and works in another owes a non-resident
    return to the work state and a resident return at home -- unless the two
    have a reciprocity agreement, in which case only the home return exists and
    the withholding to the work state is refundable in full.

    `resident_income` is what the *home* state taxes, and the caller should pass
    total income for the year. A resident state taxes income from everywhere,
    not just the wages its own W-2 line reports -- and the two W-2 lines of a
    cross-border worker usually each show the full amount, so adding the lines
    together would double-count. The other-state credit then prevents the same
    income being taxed twice.
    """
    params = state_params(year)
    home = (resident_state or "").strip().upper()
    results: list[StateResult] = []
    seen: set[str] = set()

    by_state: dict[str, dict[str, Decimal]] = {}
    for line in wage_lines:
        code = (line.get("state") or "").strip().upper()
        if not code or code not in params:
            continue
        bucket = by_state.setdefault(code, {"wages": ZERO, "withheld": ZERO})
        bucket["wages"] += money(line.get("wages", 0))
        bucket["withheld"] += money(line.get("withheld", 0))

    # --- work states (non-resident) ----------------------------------------
    for code, bucket in sorted(by_state.items()):
        if code == home:
            continue
        seen.add(code)
        if params.reciprocity_for(home, code):
            nonres = StateResult(
                code=code, name=params.get(code)["name"], kind=params.get(code)["type"],
                withheld=cents(bucket["withheld"]), is_resident=False,
            )
            nonres.balance = cents(-bucket["withheld"])
            nonres.notes.append(
                f"{home} and {code} have a reciprocity agreement, so a {home} resident owes "
                f"{code} nothing on wages. Any {code} withholding is refunded in full, and "
                f"the income is taxed at home instead. File {code} form and a {home} return."
            )
            results.append(nonres)
            continue
        nonres = compute_state(
            code, state_income=bucket["wages"], withheld=bucket["withheld"],
            filing_status=filing_status, dependents=dependents,
            federal_taxable_income=federal_taxable_income,
            federal_deduction=federal_deduction, is_resident=False,
            include_local=False, year=year,
        )
        nonres.notes.insert(0, f"Non-resident return: wages earned in {code} while living in {home}.")
        results.append(nonres)

    # --- resident state ----------------------------------------------------
    if home and home in params:
        bucket = by_state.get(home, {"wages": ZERO, "withheld": ZERO})
        if resident_income is not None:
            wages_home = money(resident_income)
        else:
            # No total supplied: the largest single line is the best available
            # proxy, since duplicated cross-border reporting makes the sum wrong.
            wages_home = max(
                [bucket["wages"]] + [b["wages"] for b in by_state.values()] or [ZERO]
            )
        resident = compute_state(
            home, state_income=wages_home, withheld=bucket["withheld"],
            filing_status=filing_status, dependents=dependents,
            federal_taxable_income=federal_taxable_income,
            federal_deduction=federal_deduction,
            long_term_gains=long_term_gains, is_resident=True, year=year,
        )
        other_state_tax = sum(
            (r.total_tax for r in results if not r.is_resident and r.total_tax > ZERO), ZERO
        )
        if other_state_tax > ZERO and resident.tax > ZERO:
            relief = min(other_state_tax, resident.tax)
            resident.credits = cents(resident.credits + relief)
            resident.line("Credit for tax paid to other states", -relief)
            resident.notes.append(
                f"{home} credits the {other_state_tax:,.0f} of tax paid to other states, so the "
                "same income is not taxed twice. The credit cannot exceed the home state's own tax."
            )
            resident.balance = cents(resident.total_tax - resident.withheld)
        results.insert(0, resident)
        seen.add(home)

    # --- withheld somewhere with no wages reported -------------------------
    for code, bucket in sorted(by_state.items()):
        if code not in seen and bucket["withheld"] > ZERO:
            results.append(compute_state(
                code, state_income=bucket["wages"], withheld=bucket["withheld"],
                filing_status=filing_status, dependents=dependents, year=year,
            ))
    return results
