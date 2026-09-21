"""What the practice charges, and why.

A preparation fee is a business decision, so the numbers live in
`config/fees.yaml` and a firm edits them without touching code. What this
module owns is the *derivation*: reading a return and deciding which tier it
falls in, which add-ons the work actually involves, and what the total comes
to.

Two rules shape it.

**The quote is itemised.** A client shown one number has no way to tell a fair
price from an arbitrary one, and a preparer who cannot explain a line has no
business charging for it. Every quote returns its lines.

**The fee never touches the tax.** It is money owed to the practice, collected
separately, and it is never deducted from a refund -- that is a loan against
the refund dressed up as convenience, and it is expensive.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from taxvault.config import fees as fee_config
from taxvault.money import ZERO, cents, money, positive


@dataclass
class FeeLine:
    label: str
    amount: Decimal
    kind: str = "add_on"      # base | add_on | adjustment | discount
    note: str = ""
    quantity: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "amount": str(cents(self.amount)),
                "kind": self.kind, "note": self.note, "quantity": self.quantity}


@dataclass
class FeeQuote:
    tier: str = "simple"
    tier_label: str = "Simple return"
    tier_description: str = ""
    base: Decimal = ZERO
    income_band: str = ""
    multiplier: Decimal = Decimal("1")
    add_ons: Decimal = ZERO
    discount: Decimal = ZERO
    total: Decimal = ZERO
    lines: list[FeeLine] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def line(self, label: str, amount: Decimal, *, kind: str = "add_on",
             note: str = "", quantity: int = 1) -> None:
        self.lines.append(FeeLine(label=label, amount=cents(amount), kind=kind,
                                  note=note, quantity=quantity))

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier, "tier_label": self.tier_label,
            "tier_description": self.tier_description,
            "base": str(cents(self.base)), "income_band": self.income_band,
            "multiplier": str(self.multiplier), "add_ons": str(cents(self.add_ons)),
            "discount": str(cents(self.discount)), "total": str(cents(self.total)),
            "lines": [line.to_dict() for line in self.lines],
            "notes": self.notes,
            "promise": fee_config().get("promise", ""),
            "collection": fee_config().get("collection", {}),
        }


def _tier_for(*, has_business: bool, has_rental: bool, state_count: int,
              has_self_employment: bool, has_investments: bool,
              itemised: bool, has_dependents: bool) -> str:
    """The first tier the return qualifies for, most complex first."""
    if has_business or has_rental or state_count > 2:
        return "complex"
    if has_self_employment:
        return "self_employed"
    if has_investments:
        return "investment"
    if itemised:
        return "itemized"
    if has_dependents or state_count >= 1:
        return "standard"
    return "simple"


def quote(
    *,
    agi: Decimal | float | str = 0,
    itemised: bool = False,
    w2_count: int = 1,
    state_count: int = 1,
    local_returns: int = 0,
    dependents: int = 0,
    self_employment_income: Decimal | float | str = 0,
    rental_income: Decimal | float | str = 0,
    capital_gains: Decimal | float | str = 0,
    investment_income: Decimal | float | str = 0,
    claims_eitc: bool = False,
    claims_education_credit: bool = False,
    prior_year_returns: int = 0,
    planning_session: bool = False,
    amended: bool = False,
    returning_client: bool = False,
    filed_early: bool = False,
) -> FeeQuote:
    """Price one engagement, itemised."""
    config = fee_config()
    result = FeeQuote()

    income = positive(money(agi))
    se = positive(money(self_employment_income))
    rental = positive(money(rental_income))
    gains = positive(money(capital_gains))
    investments = positive(money(investment_income)) + gains

    tier_key = _tier_for(
        has_business=se > ZERO and income > money(150000),
        has_rental=rental > ZERO,
        state_count=state_count,
        has_self_employment=se > ZERO,
        has_investments=investments > money(5000),
        itemised=itemised,
        has_dependents=dependents > 0,
    )
    tier = config["tiers"][tier_key]
    result.tier = tier_key
    result.tier_label = tier["label"]
    result.tier_description = tier.get("description", "")
    result.base = money(tier["base"])

    # --- income band, applied to the base only ---------------------------
    multiplier = Decimal("1")
    band_label = ""
    for band in config.get("income_bands", []):
        ceiling = band.get("up_to")
        if ceiling is None or income <= money(ceiling):
            multiplier = money(band["multiplier"])
            band_label = band.get("label", "")
            break
    result.multiplier = multiplier
    result.income_band = band_label

    adjusted_base = cents(result.base * multiplier)
    result.line(f"{tier['label']} — base", result.base, kind="base",
                note=tier.get("description", ""))
    if multiplier != Decimal("1"):
        result.line(
            f"Income band: {band_label}", adjusted_base - result.base, kind="adjustment",
            note=(f"The base is scaled by {multiplier} for this income band. Add-ons "
                  "below are not scaled."),
        )

    # --- add-ons ----------------------------------------------------------
    add_ons = config.get("add_ons", {})

    def charge(key: str, count: int = 1) -> None:
        if count <= 0:
            return
        entry = add_ons.get(key)
        if not entry:
            return
        amount = money(entry["amount"]) * count
        if amount <= ZERO and not entry.get("note"):
            return
        label = entry["label"] if count == 1 else f"{entry['label']} x{count}"
        result.add_ons += amount
        result.line(label, amount, note=entry.get("note", ""), quantity=count)

    charge("additional_w2", max(0, w2_count - 1))
    charge("additional_state", max(0, state_count - 1))
    charge("local_return", local_returns)
    charge("dependents", dependents)
    if claims_eitc:
        charge("eitc_due_diligence")
    if claims_education_credit:
        charge("education_credit")
    if gains > money(5000):
        charge("capital_gains_detail")
    charge("prior_year_return", prior_year_returns)
    if planning_session:
        charge("planning_session")
    if amended:
        charge("amended_return")

    subtotal = adjusted_base + result.add_ons

    # --- discounts ---------------------------------------------------------
    discounts = config.get("discounts", {})
    free = discounts.get("refund_only_simple", {})
    threshold = money(free.get("applies_when_agi_under", 0))
    if threshold > ZERO and income < threshold and tier_key in ("simple", "standard"):
        result.discount = subtotal
        result.line(free.get("label", "Free"), -subtotal, kind="discount",
                    note=free.get("note", ""))
        result.total = ZERO
        result.notes.append(free.get("note", "").strip())
        return _finish(result, config)

    percent = ZERO
    if returning_client:
        entry = discounts.get("returning_client", {})
        percent += money(entry.get("percent", 0))
    if filed_early:
        entry = discounts.get("simple_and_early", {})
        percent += money(entry.get("percent", 0))
    if percent > ZERO:
        reduction = cents(subtotal * percent / money(100))
        result.discount = reduction
        labels = []
        if returning_client:
            labels.append(discounts["returning_client"]["label"])
        if filed_early:
            labels.append(discounts["simple_and_early"]["label"])
        result.line(" + ".join(labels) + f" ({percent:.0f}%)", -reduction, kind="discount")

    result.total = cents(subtotal - result.discount)
    return _finish(result, config)


def _finish(result: FeeQuote, config: dict[str, Any]) -> FeeQuote:
    """Apply the floor and ceiling, and say so when either bites."""
    floor = money(config.get("minimum", 0))
    ceiling = money(config.get("maximum", 0))

    if result.total > ZERO and result.total < floor:
        result.line(f"Minimum fee", floor - result.total, kind="adjustment",
                    note=f"No engagement is priced below {floor:,.0f}.")
        result.total = floor
    if ceiling > ZERO and result.total > ceiling:
        result.line("Capped", ceiling - result.total, kind="adjustment",
                    note=f"The price list caps any single return at {ceiling:,.0f}.")
        result.total = ceiling

    result.notes.append(
        "This is the whole price. It is paid to the practice, separately from your "
        "tax, and it is never deducted from your refund."
    )
    return result


def quote_from_estimate(
    estimate: Any, *, w2_count: int = 1, prior_year_returns: int = 0,
    planning_session: bool = False, returning_client: bool = False,
    filed_early: bool = False, profile: Any = None,
) -> FeeQuote:
    """Price an engagement straight from a computed estimate.

    Reading the return rather than asking the client to describe it means the
    quote reflects the work that is actually there.
    """
    federal_result = estimate.federal
    credits = estimate.federal.credits_detail or {}

    return quote(
        agi=federal_result.agi,
        itemised=federal_result.deduction_kind == "itemised",
        w2_count=max(1, w2_count),
        state_count=len(estimate.states) or 1,
        local_returns=sum(1 for s in estimate.states if money(s.local_tax) > ZERO),
        dependents=(getattr(profile, "children_under_17", 0) or 0)
                   + (getattr(profile, "other_dependents", 0) or 0),
        self_employment_income=getattr(profile, "self_employment_income", 0) or 0,
        rental_income=getattr(profile, "rental_income", 0) or 0,
        capital_gains=(getattr(profile, "long_term_gains", 0) or 0)
                      + (getattr(profile, "short_term_gains", 0) or 0),
        investment_income=(getattr(profile, "taxable_interest", 0) or 0)
                          + (getattr(profile, "ordinary_dividends", 0) or 0),
        claims_eitc="Earned income credit" in credits,
        claims_education_credit=any("ducation" in key for key in credits),
        prior_year_returns=prior_year_returns,
        planning_session=planning_session,
        returning_client=returning_client,
        filed_early=filed_early,
    )
