"""Schedule D: netting gains and losses, and what survives to the 1040.

A capital gain is not one number. Two things decide how it is taxed, and both
have to be tracked separately all the way through:

  * **Holding period.** More than one year is long-term and gets the 0/15/20
    preferential rates; one year or less is short-term and is taxed as
    ordinary income, at up to 37%. The difference on a $50,000 gain for a
    higher-rate client is about $11,000.
  * **Character on netting.** Short-term and long-term are netted separately
    first, and only then against each other. Whichever side survives keeps
    *its* character. A $4,000 short-term loss against a $10,000 long-term gain
    leaves $6,000 of LONG-term gain -- not $6,000 of something in between.

When the two together come out negative, only $3,000 of it ($1,500 filing
separately) can be set against ordinary income in the year. That figure is
from 1978 and is not indexed, which is why a client who sold badly in one year
is often still carrying the loss a decade later. The rest carries forward
indefinitely, and it carries forward *with its character*, so the worksheet
has to remember which kind of loss is left. The IRS's own carryover worksheet
spends the $3,000 against short-term loss first, and so does this.

Nothing here touches a 401(k) or an IRA. Buying and selling inside a
retirement account is not a taxable event, produces no 1099-B, and never
reaches this module -- see `engines/retirement.py` for what those accounts do
produce.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from taxvault.config import FederalParams
from taxvault.money import ZERO, cents, money, positive


# ===========================================================================
# Input
# ===========================================================================
@dataclass
class CapitalInput:
    """The Schedule D figures, as a 1099-B and a 1099-DIV report them.

    Gains are positive and losses are negative in `short_term` and
    `long_term`, which is how a broker's year-end summary states them. The
    carryforwards are positive numbers describing a loss, because that is how
    the client thinks of them and how last year's return reports them.
    """

    short_term: Decimal = ZERO                 # net of this year's 1099-B, short-term
    long_term: Decimal = ZERO                  # net of this year's 1099-B, long-term
    capital_gain_distributions: Decimal = ZERO  # 1099-DIV box 2a: always long-term
    carryforward_short: Decimal = ZERO         # prior-year short-term loss, positive
    carryforward_long: Decimal = ZERO          # prior-year long-term loss, positive
    wash_sale_disallowed: Decimal = ZERO       # 1099-B box 1g, positive
    collectibles_gain: Decimal = ZERO          # taxed at up to 28%
    unrecaptured_1250_gain: Decimal = ZERO     # taxed at up to 25%

    def is_empty(self) -> bool:
        return all(
            money(value) == ZERO for value in asdict(self).values()
        )


# ===========================================================================
# Output
# ===========================================================================
@dataclass
class CapitalResult:
    """What Schedule D hands to Form 1040."""

    short_term_net: Decimal = ZERO
    long_term_net: Decimal = ZERO
    combined: Decimal = ZERO
    #: Short-term gain, which is taxed as ordinary income.
    ordinary_component: Decimal = ZERO
    #: Long-term gain and capital gain distributions, at preferential rates.
    preferential_component: Decimal = ZERO
    #: Gain at the 28% and 25% special rates, carved out of the above.
    collectibles_gain: Decimal = ZERO
    unrecaptured_1250_gain: Decimal = ZERO
    #: The net loss set against ordinary income this year, positive, <= 3000.
    loss_deduction: Decimal = ZERO
    #: What is left for next year, positive, by character.
    carryforward_short: Decimal = ZERO
    carryforward_long: Decimal = ZERO
    wash_sale_disallowed: Decimal = ZERO
    lines: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def net_investment_gain(self) -> Decimal:
        """What the 3.8% NIIT counts, after the allowed loss deduction."""
        return self.ordinary_component + self.preferential_component - self.loss_deduction

    @property
    def total_carryforward(self) -> Decimal:
        return self.carryforward_short + self.carryforward_long

    def line(self, label: str, amount: Decimal, *, note: str = "") -> None:
        self.lines.append(
            {"form": "Sch D", "label": label, "amount": str(cents(amount)), "note": note}
        )

    def to_dict(self) -> dict[str, Any]:
        out = {
            key: (str(value) if isinstance(value, Decimal) else value)
            for key, value in asdict(self).items()
        }
        out["net_investment_gain"] = str(cents(self.net_investment_gain))
        out["total_carryforward"] = str(cents(self.total_carryforward))
        return out


# ===========================================================================
# The calculation
# ===========================================================================
def compute_capital(
    data: CapitalInput,
    params: FederalParams,
    *,
    filing_status: str = "single",
) -> CapitalResult:
    """Net the year's gains and losses and decide what each part is taxed as."""
    result = CapitalResult()
    status = params.normalise_status(filing_status)

    short_raw = money(data.short_term)
    long_raw = money(data.long_term) + money(data.capital_gain_distributions)
    carry_short = positive(data.carryforward_short)
    carry_long = positive(data.carryforward_long)
    wash = positive(data.wash_sale_disallowed)

    # --- wash sales -------------------------------------------------------
    # A disallowed loss is added to the basis of the replacement shares, so it
    # is simply not available this year. The 1099-B reports it in box 1g and
    # the gain/loss columns are stated BEFORE the adjustment, so it has to be
    # added back. It is applied to the short-term side, which is where a
    # 30-day repurchase almost always lands.
    if wash > ZERO:
        result.wash_sale_disallowed = cents(wash)
        if short_raw < ZERO:
            applied = min(wash, -short_raw)
            short_raw += applied
            leftover = wash - applied
            if leftover > ZERO and long_raw < ZERO:
                long_raw += min(leftover, -long_raw)
        elif long_raw < ZERO:
            long_raw += min(wash, -long_raw)
        result.notes.append(
            f"{wash:,.0f} of loss is disallowed by the wash-sale rule and added to the "
            "basis of the shares you bought back. It is not lost -- you get it when you "
            "sell the replacement shares without repurchasing inside 30 days."
        )

    # --- net each character separately ------------------------------------
    short_net = cents(short_raw - carry_short)
    long_net = cents(long_raw - carry_long)
    result.short_term_net = short_net
    result.long_term_net = long_net
    if carry_short or carry_long:
        result.notes.append(
            f"A capital loss carried forward from an earlier year "
            f"({carry_short + carry_long:,.0f}) is applied first, keeping its own "
            "short-term or long-term character."
        )

    combined = cents(short_net + long_net)
    result.combined = combined
    result.line("Net short-term gain or loss", short_net, note="taxed as ordinary income")
    result.line("Net long-term gain or loss", long_net, note="preferential rates")

    # --- a net gain: whichever side survives keeps its character ----------
    if combined >= ZERO:
        if short_net >= ZERO and long_net >= ZERO:
            result.ordinary_component = short_net
            result.preferential_component = long_net
        elif short_net < ZERO:
            # A short-term loss eats into long-term gain; what is left is long-term.
            result.preferential_component = combined
            result.notes.append(
                f"A short-term loss of {-short_net:,.0f} offsets long-term gain, so the "
                f"{combined:,.0f} that remains keeps long-term treatment."
            )
        else:
            # A long-term loss eats into short-term gain; what is left is short-term.
            result.ordinary_component = combined
            result.notes.append(
                f"A long-term loss of {-long_net:,.0f} offsets short-term gain, so the "
                f"{combined:,.0f} that remains is taxed at ordinary rates."
            )
        _carve_special_rates(data, result)
        result.line("Net capital gain", combined)
        return result

    # --- a net loss: $3,000 now, the rest forever -------------------------
    cap = params.amount(
        "capital_losses",
        "ordinary_offset_cap_married_separately" if status == "married_separately"
        else "ordinary_offset_cap",
    )
    loss = -combined
    allowed = min(loss, cap)
    result.loss_deduction = cents(allowed)

    # Which character is left after the two sides offset each other.
    if short_net > ZERO:
        available_short, available_long = ZERO, loss
    elif long_net > ZERO:
        available_short, available_long = loss, ZERO
    else:
        available_short, available_long = -short_net, -long_net

    # The IRS carryover worksheet spends the allowance on short-term first.
    used_short = min(allowed, available_short)
    used_long = allowed - used_short
    result.carryforward_short = cents(positive(available_short - used_short))
    result.carryforward_long = cents(positive(available_long - used_long))

    result.line("Capital loss allowed against other income", -allowed,
                note=f"capped at {cap:,.0f} a year")
    limit_label = "1,500" if status == "married_separately" else "3,000"
    if loss > allowed:
        result.notes.append(
            f"Your net capital loss is {loss:,.0f}. Only {allowed:,.0f} can be set against "
            f"other income this year -- the limit is ${limit_label} and it is not indexed -- "
            f"so {result.total_carryforward:,.0f} carries forward. There is no time limit "
            "on using it, but it does not transfer to anyone else, so it is worth spending "
            "against future gains rather than letting it sit."
        )
        if result.carryforward_short and result.carryforward_long:
            result.notes.append(
                f"Of the carryforward, {result.carryforward_short:,.0f} stays short-term and "
                f"{result.carryforward_long:,.0f} stays long-term. Short-term loss is worth "
                "more, because it offsets income taxed at ordinary rates."
            )
    else:
        result.notes.append(
            f"Your net capital loss of {loss:,.0f} is fully deductible against other income "
            f"this year, being under the ${limit_label} annual limit."
        )
    return result


def _carve_special_rates(data: CapitalInput, result: CapitalResult) -> None:
    """Split out the 28% and 25% slices of a net long-term gain.

    Collectibles (art, coins, metals) top out at 28% and unrecaptured section
    1250 gain -- the depreciation taken on a rental -- at 25%, instead of the
    usual 20%. Neither can exceed the long-term gain actually left after
    netting, which is the part clients get wrong: a loss elsewhere in the year
    reduces these too.
    """
    room = result.preferential_component
    if room <= ZERO:
        return
    collectibles = min(positive(data.collectibles_gain), room)
    room -= collectibles
    unrecaptured = min(positive(data.unrecaptured_1250_gain), room)
    result.collectibles_gain = cents(collectibles)
    result.unrecaptured_1250_gain = cents(unrecaptured)
    if collectibles > ZERO:
        result.notes.append(
            f"{collectibles:,.0f} of gain is on collectibles, which are capped at 28% "
            "rather than 20%."
        )
    if unrecaptured > ZERO:
        result.notes.append(
            f"{unrecaptured:,.0f} is unrecaptured section 1250 gain -- depreciation you "
            "claimed on a property -- taxed at up to 25%."
        )


def describe_holding_period(days: int, params: FederalParams) -> str:
    """Short or long term, and how far off the line it is."""
    threshold = int(params.get("capital_losses", "long_term_holding_days", default=365))
    if days > threshold:
        return "long_term"
    return "short_term"


def days_to_long_term(days_held: int, params: FederalParams) -> int:
    """How many more days until a position qualifies for preferential rates."""
    threshold = int(params.get("capital_losses", "long_term_holding_days", default=365))
    return max(0, threshold + 1 - days_held)
