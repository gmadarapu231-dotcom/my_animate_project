"""Handing the client off to the place the payment is actually made.

This system works out what is owed. It does not move money, and it should not:
a federal tax payment belongs on the IRS's own channel, and a state payment on
the state's. So the last step is a handoff -- the right destination, and the
exact values to type into it.

The values matter more than the link. IRS Direct Pay is a session-based
application that takes nothing from a URL, and the two fields people get wrong
are "Reason for Payment" and "Tax Period" -- a 2025 balance posted to 2026
sits as an unapplied credit while the 2025 balance keeps accruing interest.
Handing over the exact entries removes that.

The module is also where the system says what it will *not* do. Zelle, Venmo
and gift cards are not IRS payment channels, and the IRS-impersonation scam
that has taken the most money from people is a demand for exactly those. A
product that silently omits them leaves the client to wonder; one that names
them and says why is doing its job.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from taxvault.config import federal, payments as payment_config, states as state_params
from taxvault.money import ZERO, cents, money, positive

#: What the payment is for, which decides the Direct Pay entries.
PURPOSE_BALANCE = "balance_due"
PURPOSE_ESTIMATED = "estimated"
PURPOSE_EXTENSION = "extension"
PURPOSE_INSTALMENT = "installment"


@dataclass
class HandoffField:
    """One entry the client has to make on the far side."""

    label: str
    value: str
    note: str = ""


@dataclass
class Handoff:
    destination: str = ""        # the label of where they are going
    url: str = ""
    amount: Decimal = ZERO
    jurisdiction: str = "federal"
    state_code: str = ""
    cost: str = ""
    settles_in: str = ""
    due_on: str = ""
    fields: list[HandoffField] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    alternatives: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["amount"] = str(cents(self.amount))
        return out


def _irs(key: str) -> dict[str, Any]:
    return payment_config().get("irs_links", {}).get(key, {})


def federal_handoff(
    amount: Decimal | float | str,
    *,
    year: int,
    purpose: str = PURPOSE_BALANCE,
    method: str = "irs_direct_pay",
    as_of: date | None = None,
) -> Handoff:
    """Where to pay the IRS, and what to enter when you get there."""
    config = payment_config()
    params = federal(year)
    today = as_of or date.today()
    owed = positive(money(amount))

    key = {
        "irs_direct_pay": "direct_pay", "direct_debit": "direct_pay",
        "eftps": "eftps", "card": "card", "check": "voucher",
        "installment_agreement": "online_payment_agreement",
        "short_term_extension": "online_payment_agreement",
        "long_term_direct_debit_online": "online_payment_agreement",
        "long_term_online": "online_payment_agreement",
        "long_term_phone_direct_debit": "online_payment_agreement",
        "long_term_phone": "online_payment_agreement",
    }.get(method, "direct_pay")
    link = _irs(key)

    handoff = Handoff(
        destination=link.get("label", "IRS"),
        url=link.get("url", "https://www.irs.gov/payments"),
        amount=owed,
        jurisdiction="federal",
        cost=link.get("cost", ""),
        settles_in=link.get("settles_in", ""),
        due_on=params.due_date,
    )

    entries = config.get("direct_pay_fields", {}).get(purpose, {})
    if key in ("direct_pay", "eftps"):
        handoff.fields = [
            HandoffField("Reason for Payment", entries.get("reason", "Balance Due")),
            HandoffField("Apply Payment To", entries.get("apply_to", "Income Tax - Form 1040")),
            HandoffField(
                "Tax Period for Payment", str(year),
                note=("The year the tax is FOR, not the year you are paying in. "
                      "Posting it to the wrong year leaves the balance owing and "
                      "the money sitting unapplied."),
            ),
            HandoffField("Amount", f"{owed:,.2f}"),
        ]
        handoff.steps = [
            f"Open {link.get('label', 'the IRS site')} and choose 'Make a Payment'.",
            "Enter the four values above exactly as shown.",
            "Verify your identity with a prior year's return — it asks for details "
            "from a return you have already filed, not this one.",
            "Enter your bank routing and account numbers.",
            "Save the confirmation number. It is the only proof the payment was made.",
        ]
    elif key == "card":
        handoff.steps = [
            "Open the IRS card payment page and pick one of the three authorised processors.",
            "Compare their fees — they differ, and they are charged on top of the tax.",
            f"Pay {owed:,.2f}, choosing 'Form 1040' and tax year {year}.",
            "Save the confirmation number.",
        ]
        handoff.warnings.append(
            "The processor fee buys nothing. Pay by bank transfer instead unless card "
            "rewards or a 0% promotion clearly beat it."
        )
    elif key == "voucher":
        handoff.steps = [
            "Print Form 1040-V and fill in your name, address and Social Security number.",
            f"Write a cheque for {owed:,.2f} payable to 'United States Treasury'.",
            f"Write your Social Security number and '{year} Form 1040' on the cheque.",
            "Post it to the address on the voucher for your state.",
        ]
        handoff.warnings.append(
            "The postmark is the payment date, so keep proof of postage."
        )
    else:
        handoff.steps = [
            "Open the Online Payment Agreement application.",
            "Verify your identity, then choose a short-term or long-term plan.",
            "A direct-debit plan halves the failure-to-pay penalty, from 0.5% to "
            "0.25% a month.",
            "Save the agreement number.",
        ]

    due = date.fromisoformat(params.due_date) if params.due_date else None
    if due and today > due and owed > ZERO:
        handoff.warnings.append(
            f"This balance passed its {due:%d %B %Y} deadline, so interest and the "
            "failure-to-pay penalty are already running. Paying part of it today "
            "reduces both."
        )

    handoff.alternatives = [
        {"label": entry.get("label", name), "url": entry.get("url", ""),
         "cost": entry.get("cost", ""), "note": entry.get("note", "")}
        for name, entry in config.get("irs_links", {}).items()
        if name in ("direct_pay", "eftps", "card", "online_payment_agreement", "voucher")
        and name != key
    ]
    return handoff


def state_handoff(
    amount: Decimal | float | str, *, code: str, year: int, as_of: date | None = None,
) -> Handoff:
    """Where to pay a state, which is never the IRS."""
    params = state_params(year)
    state = params.get(code)
    owed = positive(money(amount))

    handoff = Handoff(
        destination=state.get("revenue_department", f"{state['name']} revenue department"),
        url=state.get("payment_url", ""),
        amount=owed,
        jurisdiction="state",
        state_code=state["code"],
        cost="Varies by state; bank transfer is usually free",
        due_on=federal(year).due_date,
    )
    handoff.fields = [
        HandoffField("Tax type", "Individual income tax"),
        HandoffField("Tax year", str(year)),
        HandoffField("Amount", f"{owed:,.2f}"),
    ]
    handoff.steps = [
        f"Open the {handoff.destination} payment page.",
        f"Choose individual income tax for {year}.",
        f"Pay {owed:,.2f} from a bank account where you can — state card fees are "
        "usually worse than the federal ones.",
        "Save the confirmation number.",
    ]
    handoff.warnings.append(
        "A state payment is separate from the federal one. Paying the IRS does not "
        "pay your state, and the two have their own deadlines and penalties."
    )
    if state["type"] == "none":
        handoff.warnings.append(
            f"{state['name']} has no income tax on wages, so there should be nothing "
            "to pay here. Check the figure before sending anything."
        )
    if not handoff.url:
        # Better to say where to look than to render a link to nowhere.
        handoff.steps = [
            f"Search for the {handoff.destination} online payment service — this "
            "system does not hold a verified address for it.",
            "Confirm you are on a .gov address before entering anything.",
        ]
        handoff.warnings.append(
            "No payment address is on file for this state. Go to the state's own "
            "revenue site rather than following a link from anywhere else."
        )
    return handoff


def service_fee_options() -> list[dict[str, Any]]:
    """How the client settles the preparation fee. Zelle lives here.

    Kept apart from the tax deliberately. These pay a preparer; none of them
    can reach a government tax account, and blurring the two is how people end
    up sending money to a stranger who asked for it by Zelle.
    """
    config = payment_config().get("service_fee", {})
    out: list[dict[str, Any]] = []
    for key, entry in config.items():
        out.append({
            "method": key,
            "label": entry.get("label", key),
            "cost": entry.get("cost", ""),
            "settles_in": entry.get("settles_in", ""),
            "irreversible": bool(entry.get("irreversible", False)),
            "note": entry.get("note", ""),
            "recommended": key == "ach_debit",
        })
    return out


def refused_methods() -> list[dict[str, str]]:
    """What the IRS will not take, and what to use instead.

    Shown rather than omitted. "It is not in the list" and "it does not work"
    are different answers, and only one of them protects someone about to be
    talked into a Zelle transfer by a caller claiming to be the IRS.
    """
    return list(payment_config().get("not_accepted_by_irs", []))


def scam_warning() -> dict[str, str]:
    return {
        "headline": "How to know a tax demand is fake",
        "body": (
            "The IRS makes first contact by post, never by phone call, text or email. "
            "It never demands payment by Zelle, Venmo, Cash App, cryptocurrency, wire "
            "transfer or gift card, and it never threatens arrest or deportation over "
            "the phone. Every legitimate federal payment goes through irs.gov or "
            "eftps.gov — type the address yourself rather than following a link "
            "someone sent you."
        ),
        "url": "https://www.irs.gov/newsroom/tax-scams-consumer-alerts",
    }
