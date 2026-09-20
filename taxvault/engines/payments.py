"""How the money moves: refunds out, balances in, and what each route costs.

The question a client actually asks is not "what do I owe" but "what do I pay,
when, and what does it cost me to spread it". So every option is priced to a
**total cost**, not just a monthly figure -- a 72-month plan with a low payment
can cost more in penalties and interest than the balance it is spreading, and
that has to be visible before someone signs up to it.

Three things this module refuses to fudge:

* An extension extends the time to *file*, never the time to *pay*. Interest
  and the failure-to-pay penalty run from April regardless.
* A long-term instalment agreement halves the failure-to-pay penalty (0.5% to
  0.25% a month), which is a real benefit and is modelled.
* Paying by card costs a percentage fee with no tax benefit. It is presented
  with the fee attached rather than as a neutral choice.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from taxvault.config import federal, payments as payment_config
from taxvault.enums import PaymentDirection, PaymentMethod
from taxvault.money import ZERO, cents, money, pct, positive


@dataclass
class PaymentOption:
    method: str
    label: str
    amount: Decimal = ZERO
    instalments: int = 1
    instalment_amount: Decimal = ZERO
    setup_fee: Decimal = ZERO
    processing_fee: Decimal = ZERO
    penalty: Decimal = ZERO
    interest: Decimal = ZERO
    total_cost: Decimal = ZERO          # amount + every charge
    first_due_on: str = ""
    final_due_on: str = ""
    available: bool = True
    recommended: bool = False
    schedule: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def cost_of_delay(self) -> Decimal:
        return cents(self.setup_fee + self.processing_fee + self.penalty + self.interest)

    def to_dict(self) -> dict[str, Any]:
        out = {
            key: (str(value) if isinstance(value, Decimal) else value)
            for key, value in asdict(self).items()
        }
        out["cost_of_delay"] = str(self.cost_of_delay)
        return out


def _accrue(balance: Decimal, months: int, *, monthly_penalty: Decimal,
            annual_interest: Decimal, penalty_cap: Decimal,
            amortising: bool) -> tuple[Decimal, Decimal]:
    """Penalty and interest over `months`, on a balance that may be shrinking.

    A plan pays the balance down, so charging the full balance for the whole
    term would overstate the cost. The average outstanding balance over a level
    repayment is about half the opening figure, which is the approximation used
    here and stated in the notes rather than hidden.
    """
    if balance <= ZERO or months <= 0:
        return ZERO, ZERO
    average = balance / 2 if amortising else balance
    penalty = min(balance * monthly_penalty * months, balance * penalty_cap)
    interest = average * annual_interest * money(months) / money(12)
    return cents(penalty), cents(interest)


def build_payment_options(
    balance: Decimal | float | str,
    *,
    year: int,
    as_of: date | None = None,
    due_date: date | None = None,
    jurisdiction: str = "federal",
    state_code: str = "",
    can_pay_in_full: bool = True,
) -> tuple[str, list[PaymentOption]]:
    """Returns `(direction, options)` -- refund routes or ways to settle a balance."""
    params = federal(year)
    config = payment_config()
    today = as_of or date.today()
    due = due_date or (date.fromisoformat(params.due_date) if params.due_date else date(year + 1, 4, 15))
    amount = money(balance)

    # --- refund ------------------------------------------------------------
    if amount < ZERO:
        refund = positive(-amount)
        options: list[PaymentOption] = []
        for key, entry in config["refund"].items():
            option = PaymentOption(
                method=key, label=entry["label"], amount=refund,
                instalment_amount=refund, total_cost=refund,
                notes=[entry["note"]],
            )
            days = int(entry.get("typical_days", 0))
            # Zero means there is nothing to wait for (the credit just carries
            # forward), so leave the date blank rather than printing today's.
            option.first_due_on = (today + timedelta(days=days)).isoformat() if days else ""
            options.append(option)
        options[0].recommended = True
        options[0].notes.append(
            "Check the routing and account numbers character by character: a deposit sent "
            "to a wrong but valid account is not recoverable by the IRS."
        )
        return PaymentDirection.REFUND.value, options

    if amount == ZERO:
        return PaymentDirection.EVEN.value, [
            PaymentOption(method="none", label="Nothing to pay",
                          notes=["Withholding matched the tax almost exactly."])
        ]

    # --- balance due --------------------------------------------------------
    late = today > due
    annual_interest = params.rate("penalties_and_interest", "underpayment_interest_annual")
    ftp_rate = params.rate("penalties_and_interest", "failure_to_pay_monthly")
    ftp_plan_rate = params.rate("penalties_and_interest", "failure_to_pay_with_installment")
    ftp_cap = params.rate("penalties_and_interest", "failure_to_pay_cap")

    options = []

    # 1. pay in full
    for key in ("direct_debit", "irs_direct_pay", "eftps", "check", "card"):
        entry = config["balance_due"][key]
        option = PaymentOption(
            method=key, label=entry["label"], amount=amount, instalments=1,
            instalment_amount=amount, first_due_on=due.isoformat(),
            final_due_on=due.isoformat(), notes=[entry["note"]],
            available=can_pay_in_full,
        )
        if entry.get("fee_rate"):
            option.processing_fee = max(
                cents(amount * pct(entry["fee_rate"])), money(entry.get("fee_minimum", 0))
            )
            option.notes.append(
                f"The fee on {amount:,.0f} is about {option.processing_fee:,.2f}, which buys "
                "nothing unless card rewards beat it."
            )
        if late:
            months_late = max(1, (today.year - due.year) * 12 + today.month - due.month)
            penalty, interest = _accrue(
                amount, months_late, monthly_penalty=ftp_rate,
                annual_interest=annual_interest, penalty_cap=ftp_cap, amortising=False,
            )
            option.penalty, option.interest = penalty, interest
            option.first_due_on = today.isoformat()
            option.notes.append(
                f"This balance is already {months_late} month(s) past the {due:%d %B %Y} "
                "deadline, so penalty and interest are included."
            )
        option.total_cost = cents(
            amount + option.setup_fee + option.processing_fee + option.penalty + option.interest
        )
        options.append(option)

    # 2. short-term plan
    short = config["installment"]["short_term"]
    if amount <= money(short["max_balance"]):
        months = 6
        penalty, interest = _accrue(
            amount, months, monthly_penalty=ftp_rate, annual_interest=annual_interest,
            penalty_cap=ftp_cap, amortising=True,
        )
        option = PaymentOption(
            method=PaymentMethod.SHORT_TERM_EXTENSION.value, label=short["label"],
            amount=amount, instalments=months, instalment_amount=cents(amount / months),
            penalty=penalty, interest=interest,
            first_due_on=(max(today, due)).isoformat(),
            final_due_on=(max(today, due) + timedelta(days=int(short["max_days"]))).isoformat(),
            notes=[short["note"],
                   "No setup fee, and it can be arranged online in minutes."],
        )
        option.total_cost = cents(amount + penalty + interest)
        options.append(option)

    # 3. long-term instalment agreements
    for key in ("long_term_direct_debit_online", "long_term_online",
                "long_term_phone_direct_debit", "long_term_phone"):
        entry = config["installment"][key]
        if amount > money(entry["max_balance"]):
            options.append(PaymentOption(
                method=key, label=entry["label"], amount=amount, available=False,
                notes=[f"Not available: the balance of {amount:,.0f} is above the "
                       f"{money(entry['max_balance']):,.0f} limit for this plan. Larger "
                       "balances need Form 433 financial disclosure."],
            ))
            continue
        months = int(entry["max_months"])
        # Keep the term inside what the collection statute allows.
        instalment = cents(amount / months)
        rate = ftp_plan_rate if entry.get("reduces_failure_to_pay") else ftp_rate
        penalty, interest = _accrue(
            amount, months, monthly_penalty=rate, annual_interest=annual_interest,
            penalty_cap=ftp_cap, amortising=True,
        )
        fee = money(entry["setup_fee"])
        start = max(today, due) + timedelta(days=30)
        option = PaymentOption(
            method=key, label=entry["label"], amount=amount, instalments=months,
            instalment_amount=instalment, setup_fee=fee, penalty=penalty, interest=interest,
            first_due_on=start.isoformat(),
            final_due_on=(start + timedelta(days=30 * months)).isoformat(),
            notes=[entry.get("note", ""), config["installment"]["low_income_waiver_note"]],
        )
        option.total_cost = cents(amount + fee + penalty + interest)
        option.schedule = [
            {"due_on": (start + timedelta(days=30 * i)).isoformat(),
             "amount": str(instalment)}
            for i in range(min(months, 12))
        ]
        if months > 12:
            option.notes.append(f"Schedule shows the first 12 of {months} payments.")
        option.notes = [n for n in option.notes if n]
        options.append(option)

    # 4. offer in compromise, only where it is plausible
    oic = config["offer_in_compromise"]
    if amount > money(10000):
        options.append(PaymentOption(
            method="offer_in_compromise", label="Offer in compromise", amount=amount,
            setup_fee=money(oic["application_fee"]), available=True,
            total_cost=amount,
            notes=[oic["note"],
                   "Worth exploring only where the balance genuinely cannot be paid from "
                   "income or assets. Check eligibility with the IRS pre-qualifier first."],
        ))

    # --- recommendation ----------------------------------------------------
    payable = [o for o in options if o.available and o.method != "offer_in_compromise"]
    if payable:
        if can_pay_in_full:
            best = next((o for o in payable if o.method == "irs_direct_pay"), payable[0])
        else:
            best = min(payable, key=lambda o: o.total_cost)
        best.recommended = True
        best.notes.append("Lowest total cost of the routes available here.")

    if late:
        for option in options:
            option.notes.append(
                "Filing the return stops the failure-to-file penalty even if the balance "
                "cannot be paid. Never delay the return because of the money."
            )
    if jurisdiction == "state" and state_code:
        for option in options:
            option.notes.append(
                f"These figures use federal penalty and interest rates. {state_code} sets its "
                "own, and its instalment terms differ -- confirm with the state before relying "
                "on the total."
            )
    return PaymentDirection.BALANCE_DUE.value, options


def estimated_payments_for_next_year(
    *,
    current_year_tax: Decimal | float | str,
    current_year_agi: Decimal | float | str,
    expected_withholding: Decimal | float | str = 0,
    year: int,
) -> dict[str, Any]:
    """The safe harbour, and the quarterly figure that reaches it.

    Two ways to be safe: pay 90% of what next year actually turns out to be, or
    100% of this year's tax -- 110% where AGI passed $150,000. The prior-year
    route is the useful one, because it is knowable in advance.
    """
    params = federal(year)
    tax = positive(money(current_year_tax))
    agi = money(current_year_agi)
    withholding = positive(money(expected_withholding))

    high_income = agi > params.amount("penalties_and_interest", "high_income_agi_threshold")
    # `amount`, not `rate`: 1.10 here is a multiplier (110% of last year's tax),
    # and the percentage heuristic in `pct` would read it as 1.1%.
    multiplier = params.amount(
        "penalties_and_interest",
        "estimated_tax_safe_harbor_prior_high_income" if high_income
        else "estimated_tax_safe_harbor_prior",
        default=1,
    )
    if multiplier <= ZERO:
        multiplier = money(1)
    target = cents(tax * multiplier)
    shortfall = positive(target - withholding)
    quarterly = cents(shortfall / 4)
    due_dates = params.get("estimated_payment_due_dates", default=[])

    return {
        "safe_harbor_basis": "prior_year",
        "multiplier": str(multiplier),
        "high_income": high_income,
        "target_payments": str(target),
        "expected_withholding": str(withholding),
        "shortfall": str(shortfall),
        "quarterly_amount": str(quarterly),
        "due_dates": due_dates,
        "schedule": [
            {"due_on": due, "amount": str(quarterly)} for due in due_dates
        ] if shortfall > ZERO else [],
        "note": (
            f"Paying {target:,.0f} across the year -- {multiplier:.0%} of this year's tax -- "
            "avoids the underpayment penalty no matter what next year turns out to be. "
            + ("The 110% figure applies because AGI passed $150,000. " if high_income else "")
            + "Withholding counts as paid evenly through the year whenever it happens, so "
            "raising withholding late can still repair a shortfall that an estimated "
            "payment cannot."
        ),
    }
