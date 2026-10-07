"""What the platform earns on a return, and the model it refuses to offer.

A percentage of the refund is the obvious way to monetise a tax product and it
is prohibited. Circular 230 section 10.27(b)(1) bars a contingent fee for
preparing an original return, and 10.27(c)(1) names "a percentage of the
refund" as exactly that. The sanction is suspension or disbarment from
practice before the IRS -- against the individual's PTIN as well as the firm,
so it ends the practice, not just the product line.

The rule exists for a reason worth taking seriously on its own merits: a
preparer paid more when the refund is larger has a direct financial interest
in a larger refund, which is precisely the interest the client is trusting
them not to have.

So `quote_platform_fee` raises on a refund-percentage configuration rather
than computing one, and the error carries the citation and the nearest legal
alternative. Everything else here is a fee that can be quoted before the work
starts and does not move with the outcome:

  * **per_return** -- a flat platform fee per return, charged to the firm.
  * **revenue_share** -- a share of the preparation fee the firm charges.
  * **subscription** -- per seat per month, with an overage.

All three are billed to the FIRM, not to the taxpayer. The taxpayer's price is
`engines/fees.py`, driven by how much work the return takes.
"""

from __future__ import annotations

import functools
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from taxvault.money import ZERO, cents, money, positive

CONFIG = Path(__file__).resolve().parent.parent / "config" / "commission.yaml"

#: Model keys that are configured only so they can be refused.
PROHIBITED = frozenset({"refund_percentage"})


class ProhibitedFeeModel(ValueError):
    """A fee model that would breach Circular 230. Raised, never computed."""


@functools.lru_cache(maxsize=1)
def commission_config() -> dict[str, Any]:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


@dataclass
class PlatformFee:
    """What the platform earns on one return, and how it was arrived at."""

    model: str = ""
    label: str = ""
    amount: Decimal = ZERO
    billed_to: str = "firm"
    basis: str = ""
    lines: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def line(self, label: str, amount: Decimal, note: str = "") -> None:
        self.lines.append(
            {"label": label, "amount": str(cents(amount)), "note": note}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            key: (str(cents(value)) if isinstance(value, Decimal) else value)
            for key, value in asdict(self).items()
        }


def active_model() -> str:
    return str(commission_config().get("active", "per_return"))


def model_config(name: str = "") -> dict[str, Any]:
    models = commission_config().get("models", {}) or {}
    key = name or active_model()
    if key not in models:
        raise ValueError(f"{key!r} is not a configured commission model")
    return {"key": key, **models[key]}


def quote_platform_fee(
    *,
    model: str = "",
    preparation_fee: Decimal | float | str = 0,
    returns_this_period: int = 0,
    seats: int = 1,
    returns_included: int | None = None,
    refund: Decimal | float | str | None = None,
) -> PlatformFee:
    """Price the platform's own take on one return.

    `refund` is accepted and deliberately unused: a caller passing it is
    probably reaching for a refund-based fee, and this is where to say no.
    """
    config = model_config(model)
    key = config["key"]

    if key in PROHIBITED or config.get("allowed") is False:
        raise ProhibitedFeeModel(_refusal(config))

    fee = PlatformFee(
        model=key,
        label=str(config.get("label", key)),
        billed_to=str(config.get("billed_to", "firm")),
    )

    if key == "per_return":
        amount = money(config.get("amount", 0))
        breaks = config.get("volume_breaks") or []
        for row in sorted(breaks, key=lambda r: int(r.get("from_returns", 0))):
            if returns_this_period >= int(row.get("from_returns", 0)):
                amount = money(row.get("amount", amount))
        fee.amount = cents(amount)
        fee.basis = f"flat fee per return at {returns_this_period:,} returns this period"
        fee.line("Platform fee for this return", fee.amount)
        if breaks:
            nxt = next(
                (row for row in sorted(breaks, key=lambda r: int(r.get("from_returns", 0)))
                 if int(row.get("from_returns", 0)) > returns_this_period),
                None,
            )
            if nxt:
                need = int(nxt["from_returns"]) - returns_this_period
                fee.notes.append(
                    f"{need:,} more returns this period brings this down to "
                    f"{money(nxt['amount']):,.2f} each."
                )

    elif key == "revenue_share":
        base = positive(preparation_fee)
        rate = money(config.get("rate", 0))
        raw = base * rate
        floor = money(config.get("minimum", 0))
        ceiling = money(config.get("maximum", 0))
        amount = max(raw, floor) if floor else raw
        if ceiling:
            amount = min(amount, ceiling)
        fee.amount = cents(amount)
        fee.basis = f"{rate:.0%} of the {base:,.2f} preparation fee"
        fee.line(f"{rate:.0%} of the preparation fee", cents(raw))
        if floor and raw < floor:
            fee.line("Minimum applied", cents(floor - raw))
        if ceiling and raw > ceiling:
            fee.line("Capped", -cents(raw - ceiling))
        fee.notes.append(
            "A share of the preparation fee, which was fixed before the return was "
            "worked out. It is not affected by the refund and it is charged to the "
            "firm, not the taxpayer."
        )

    elif key == "subscription":
        per_seat = money(config.get("monthly_per_seat", 0))
        included = (
            returns_included
            if returns_included is not None
            else int(config.get("included_returns_per_seat", 0)) * max(1, seats)
        )
        overage_rate = money(config.get("overage_per_return", 0))
        over = max(0, returns_this_period - included)
        # Per-return cost of this one return: the overage rate, or nothing when
        # the subscription already covers it.
        fee.amount = cents(overage_rate if over > 0 else ZERO)
        fee.basis = (
            f"{returns_this_period:,} of {included:,} included returns used"
            + (f"; {over:,} over" if over else "")
        )
        fee.line(f"Subscription, {seats} seat(s)", cents(per_seat * max(1, seats)),
                 note="billed monthly, not per return")
        if over > 0:
            fee.line("Overage for this return", fee.amount)
            fee.notes.append(
                f"The subscription covers {included:,} returns and this is number "
                f"{returns_this_period:,}, so it is billed at {overage_rate:,.2f}."
            )
        else:
            fee.notes.append(
                f"Covered by the subscription: {included - returns_this_period:,} of "
                f"{included:,} included returns are still unused."
            )

    else:  # pragma: no cover - a model added to YAML without code
        raise ValueError(f"{key!r} has no pricing implementation")

    if refund is not None:
        fee.notes.append(
            "The refund was supplied and ignored. This fee cannot depend on it: "
            "a fee based on the refund is a contingent fee, prohibited by "
            "Circular 230 section 10.27."
        )
    return fee


def _refusal(config: dict[str, Any]) -> str:
    refusal = str(config.get("refusal", "")).strip()
    instead = str(config.get("instead", "")).strip()
    parts = [f"The {config.get('label', config['key'])!r} fee model is not available."]
    if refusal:
        parts.append(refusal)
    if instead:
        parts.append("Instead: " + instead)
    return "\n\n".join(parts)


def refund_handling() -> dict[str, Any]:
    """Whether a fee may be taken out of a refund. It may not."""
    config = commission_config().get("deduct_from_refund", {}) or {}
    return {
        "allowed": bool(config.get("allowed", False)),
        "refusal": str(config.get("refusal", "")).strip(),
        "instead": str(config.get("instead", "")).strip(),
    }


def disclosure() -> dict[str, Any]:
    """What a client must be shown before any work starts."""
    config = commission_config().get("disclosure", {}) or {}
    return {
        "before_work_starts": list(config.get("before_work_starts", [])),
        "on_every_quote": str(config.get("on_every_quote", "")).strip(),
    }


def describe_models() -> dict[str, Any]:
    """Every model, including the refused one, for a pricing screen."""
    models = commission_config().get("models", {}) or {}
    out = []
    for key, config in models.items():
        allowed = key not in PROHIBITED and config.get("allowed") is not False
        row: dict[str, Any] = {
            "key": key,
            "label": config.get("label", key),
            "description": str(config.get("description", "")).strip(),
            "allowed": allowed,
            "active": key == active_model(),
            "billed_to": config.get("billed_to", "firm"),
        }
        if not allowed:
            row["refusal"] = str(config.get("refusal", "")).strip()
            row["instead"] = str(config.get("instead", "")).strip()
        out.append(row)
    return {
        "active": active_model(),
        "currency": commission_config().get("currency", "USD"),
        "models": out,
        "deduct_from_refund": refund_handling(),
        "disclosure": disclosure(),
    }
