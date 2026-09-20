"""Money, rounding, and bracket arithmetic.

Every figure in this system is a `Decimal`. Tax is not a domain where a float
is acceptable: the IRS specifies exact cents, and 0.1 + 0.2 != 0.3 is a real
notice from the IRS, not a curiosity.

Two rounding rules are implemented because the IRS uses two:

* `cents`  -- half-up to 0.01, used for every intermediate figure.
* `whole`  -- half-up to $1, used where a form line says "round to the nearest
              dollar" (most of Form 1040). Rounding is applied at the *line*,
              never mid-calculation, which is why it is a separate call.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Sequence

ZERO = Decimal("0")
CENT = Decimal("0.01")
ONE = Decimal("1")


def money(value: object) -> Decimal:
    """Coerce anything sane into a `Decimal` amount.

    `str(value)` rather than `Decimal(float)` on purpose: `Decimal(0.1)` is
    0.1000000000000000055511151231257827, while `Decimal("0.1")` is 0.1.
    """
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return ZERO
    if isinstance(value, bool):  # bool is an int subclass; refuse it explicitly
        raise TypeError("a boolean is not an amount")
    return Decimal(str(value))


def cents(value: object) -> Decimal:
    return money(value).quantize(CENT, rounding=ROUND_HALF_UP)


def whole(value: object) -> Decimal:
    """Round to the nearest dollar, the way a 1040 line does."""
    return money(value).quantize(ONE, rounding=ROUND_HALF_UP)


def positive(value: object) -> Decimal:
    """Clamp at zero. Tax lines say 'if zero or less, enter -0-' constantly."""
    amount = money(value)
    return amount if amount > ZERO else ZERO


def total(values: Iterable[object]) -> Decimal:
    out = ZERO
    for value in values:
        out += money(value)
    return out


def pct(value: object) -> Decimal:
    """A percentage given as either 4.4 or 0.044 -> Decimal('0.044').

    Rate tables are written by humans, and humans write state rates as `4.4`
    and federal rates as `0.22`. Anything above 1 is read as a percentage,
    which is unambiguous because no income tax rate is 100%.
    """
    rate = money(value)
    return rate / Decimal(100) if rate > ONE else rate


@dataclass(frozen=True)
class Bracket:
    """One row of a rate schedule: `rate` applies above `floor`, up to `ceiling`."""

    floor: Decimal
    ceiling: Decimal | None  # None == no upper bound
    rate: Decimal

    @property
    def width(self) -> Decimal | None:
        return None if self.ceiling is None else self.ceiling - self.floor


def brackets_from(rows: Sequence[dict]) -> list[Bracket]:
    """Build a schedule from YAML rows of `{up_to, rate}`, ordered ascending.

    `up_to` is the top of the band, which is how published rate tables read
    ("$11,925 ... 10%"), and omitting it means the final unbounded band.
    """
    out: list[Bracket] = []
    floor = ZERO
    for row in rows:
        raw_ceiling = row.get("up_to")
        ceiling = None if raw_ceiling in (None, "", "inf") else money(raw_ceiling)
        if ceiling is not None and ceiling <= floor:
            raise ValueError(f"bracket ceiling {ceiling} does not exceed floor {floor}")
        out.append(Bracket(floor=floor, ceiling=ceiling, rate=pct(row["rate"])))
        if ceiling is None:
            break
        floor = ceiling
    if not out:
        raise ValueError("a rate schedule needs at least one bracket")
    if out[-1].ceiling is not None:
        raise ValueError("the last bracket must be unbounded (omit `up_to`)")
    return out


def tax_on(amount: object, schedule: Sequence[Bracket]) -> Decimal:
    """Progressive tax on `amount`. Only the slice inside each band is taxed."""
    taxable = positive(amount)
    due = ZERO
    for band in schedule:
        if taxable <= band.floor:
            break
        top = taxable if band.ceiling is None else min(taxable, band.ceiling)
        due += (top - band.floor) * band.rate
    return cents(due)


def marginal_rate(amount: object, schedule: Sequence[Bracket]) -> Decimal:
    """The rate the next dollar would meet."""
    taxable = positive(amount)
    rate = schedule[0].rate
    for band in schedule:
        if taxable >= band.floor:
            rate = band.rate
        else:
            break
    return rate


def phase_out(
    benefit: object,
    magi: object,
    threshold: object,
    *,
    rate: object = "0.05",
    step: object = "1000",
    floor_amount: object = "0",
) -> Decimal:
    """Reduce `benefit` once income passes `threshold`.

    Congress writes phase-outs two ways and this covers both: a continuous
    percentage (`step=1`, e.g. the senior deduction's 6%) and a stepped one
    ("$50 for each $1,000 or fraction thereof", e.g. the Child Tax Credit).
    `floor_amount` is the level the benefit never falls below, which the SALT
    cap needs.
    """
    excess = positive(money(magi) - money(threshold))
    if excess <= ZERO:
        return cents(benefit)
    stride = money(step)
    # "or fraction thereof": a partial step counts as a whole one.
    steps = (excess / stride).to_integral_value(rounding="ROUND_CEILING") if stride > ONE else excess
    reduction = steps * stride * pct(rate) if stride > ONE else excess * pct(rate)
    reduced = money(benefit) - reduction
    return cents(max(reduced, money(floor_amount)))
