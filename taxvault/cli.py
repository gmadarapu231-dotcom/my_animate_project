"""Command line: run the server, estimate a return, inspect the rate tables.

The CLI exists so the engine can be exercised without a browser, which matters
for two people: whoever reviews the rate tables each January, and whoever is
debugging a client's return and wants the figures without the UI in the way.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from decimal import Decimal
from typing import Any

from taxvault.config import federal, latest_year, states, supported_years
from taxvault.engines.estimate import run_estimate
from taxvault.engines.federal import TaxProfile
from taxvault.engines.payments import build_payment_options
from taxvault.engines.planning import build_strategies
from taxvault.money import money


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("taxvault.api.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_states(args: argparse.Namespace) -> int:
    params = states(args.year)
    if args.code:
        _print_json(params.get(args.code))
        return 0
    none = params.no_tax_states()
    flat = [c for c in params.codes() if params.get(c)["type"] == "flat"]
    graduated = [c for c in params.codes() if params.get(c)["type"] == "graduated"]
    print(f"Tax year {params.year} — {len(params.codes())} jurisdictions\n")
    print(f"No income tax ({len(none)}):")
    print("  " + "  ".join(none))
    print(f"\nFlat rate ({len(flat)}):")
    for code in flat:
        state = params.get(code)
        print(f"  {code}  {float(state['rate']):>7.2%}  {state['name']}")
    print(f"\nGraduated ({len(graduated)}):")
    for code in graduated:
        schedule = params.brackets(code, "single")
        print(f"  {code}  {float(schedule[0].rate):>6.2%} – {float(schedule[-1].rate):<6.2%} "
              f"{params.get(code)['name']}")
    print(f"\n{params.disclaimer}")
    return 0


def cmd_limits(args: argparse.Namespace) -> int:
    params = federal(args.year)
    print(f"Tax year {params.year} — {params.source}")
    print(f"Due {params.due_date}, extended {params.extended_due_date}\n")
    print("Standard deduction:")
    for status in params.statuses:
        print(f"  {status:<28} {float(params.amount('standard_deduction', status)):>10,.0f}")
    print("\nBrackets (single):")
    for band in params.brackets("single"):
        top = "and above" if band.ceiling is None else f"to {float(band.ceiling):,.0f}"
        print(f"  {float(band.floor):>10,.0f} {top:<18} {float(band.rate):>6.0%}")
    print("\nContribution limits:")
    for key, value in (params.get("contribution_limits", default={}) or {}).items():
        if isinstance(value, (int, float)):
            print(f"  {key:<32} {value:>10,.0f}")
    return 0


def _profile_from_args(args: argparse.Namespace) -> TaxProfile:
    return TaxProfile(
        tax_year=args.year or latest_year(),
        filing_status=args.status,
        resident_state=(args.state or "").upper(),
        wages=money(args.wages),
        federal_withheld=money(args.withheld),
        social_security_wages=money(args.wages),
        age=args.age,
        spouse_age=args.spouse_age,
        children_under_17=args.children,
        self_employment_income=money(args.self_employment),
        qbi_income=money(args.self_employment),
        long_term_gains=money(args.long_term_gains),
        taxable_interest=money(args.interest),
        state_local_income_tax=money(args.state_tax_paid),
        property_tax=money(args.property_tax),
        mortgage_interest=money(args.mortgage_interest),
        charitable_cash=money(args.charitable),
    )


def cmd_estimate(args: argparse.Namespace) -> int:
    profile = _profile_from_args(args)
    lines = [{"state": profile.resident_state, "wages": profile.wages,
              "withheld": money(args.state_withheld)}] if profile.resident_state else []
    result = run_estimate(
        profile, wage_lines=lines, method=args.method,
        planning_context={"existing_401k": money(args.existing_401k),
                          "has_hdhp": args.hdhp, "hdhp_family": args.hdhp_family},
    )
    if args.json:
        _print_json(result.to_dict())
        return 0

    federal_result = result.federal
    print(f"\n{result.headline()}")
    print(f"Tax year {result.tax_year} · {result.filing_status} · {result.method} method\n")
    print(f"  {'Adjusted gross income':<34} {float(federal_result.agi):>12,.0f}")
    print(f"  {'Deductions':<34} {float(federal_result.deduction_taken):>12,.0f}"
          f"  ({federal_result.deduction_kind})")
    print(f"  {'Taxable income':<34} {float(federal_result.taxable_income):>12,.0f}")
    print(f"  {'Federal tax':<34} {float(federal_result.total_tax):>12,.0f}")
    print(f"  {'Paid in':<34} {float(federal_result.total_payments):>12,.0f}")
    label = "Federal owed" if federal_result.balance > 0 else "Federal refund"
    print(f"  {label:<34} {abs(float(federal_result.balance)):>12,.0f}")
    for state in result.states:
        print(f"\n  {state.name} ({state.kind})")
        print(f"    {'Tax':<32} {float(state.total_tax):>12,.0f}")
        print(f"    {'Withheld':<32} {float(state.withheld):>12,.0f}")
        label = "Owed" if state.balance > 0 else "Refund"
        print(f"    {label:<32} {abs(float(state.balance)):>12,.0f}")
        for note in state.notes[:2]:
            print(f"    · {note}")
    print(f"\n  {'TOTAL':<34} "
          f"{'owed' if result.total_balance > 0 else 'refund'} "
          f"{abs(float(result.total_balance)):,.0f}")
    print(f"  Effective {float(federal_result.effective_rate):.2%} · "
          f"marginal {float(federal_result.marginal_rate):.0%}")

    if result.strategies:
        print("\nPlanning options:")
        for strategy in result.strategies:
            mark = "open" if strategy.still_available else "shut"
            kind = "   " if strategy.actionable else "fyi"
            print(f"  [{mark}] {kind} {strategy.label:<44} "
                  f"{float(strategy.total_saving):>9,.0f}"
                  f"{'  closes ' + strategy.closes_on.isoformat() if strategy.closes_on else ''}")
        if result.baseline:
            print(f"\n  Filing as-is: {float(result.baseline.total_tax):,.0f}  ->  "
                  f"with planning: {float(result.total_tax):,.0f}  "
                  f"(saving {float(result.saving_against_baseline):,.0f})")

    print("\nWarnings:" if result.warnings else "", end="")
    for warning in result.warnings:
        print(f"\n  {warning['severity'].upper()} box {warning.get('box', '')}: {warning['message']}")
    print("\n  Estimates only. Not an IRS e-file provider; confirm before filing.\n")
    return 0


def cmd_payment(args: argparse.Namespace) -> int:
    direction, options = build_payment_options(
        args.balance, year=args.year or latest_year(),
        can_pay_in_full=not args.cannot_pay,
    )
    print(f"\n{direction.replace('_', ' ')} of {abs(float(args.balance)):,.2f}\n")
    for option in options:
        flag = "*" if option.recommended else (" " if option.available else "x")
        extra = f" (+{float(option.cost_of_delay):,.2f})" if option.cost_of_delay else ""
        plan = f"{option.instalments}x {float(option.instalment_amount):,.2f}" if option.instalments > 1 else "in full"
        print(f" {flag} {option.label:<46} {plan:<20} total {float(option.total_cost):>11,.2f}{extra}")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="taxvault",
        description="US tax estimation and filing preparation. Estimates only — "
                    "this is not an IRS e-file provider.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the API and the web/mobile client")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve)

    state_cmd = sub.add_parser("states", help="the state rate tables")
    state_cmd.add_argument("code", nargs="?", help="one state, in detail")
    state_cmd.add_argument("--year", type=int)
    state_cmd.set_defaults(func=cmd_states)

    limits = sub.add_parser("limits", help="federal brackets, deductions and limits")
    limits.add_argument("--year", type=int)
    limits.set_defaults(func=cmd_limits)

    est = sub.add_parser("estimate", help="estimate a return from the command line")
    est.add_argument("--wages", default=0)
    est.add_argument("--withheld", default=0)
    est.add_argument("--state", default="")
    est.add_argument("--state-withheld", default=0)
    est.add_argument("--status", default="single")
    est.add_argument("--year", type=int)
    est.add_argument("--age", type=int, default=40)
    est.add_argument("--spouse-age", type=int, default=0)
    est.add_argument("--children", type=int, default=0)
    est.add_argument("--self-employment", default=0)
    est.add_argument("--long-term-gains", default=0)
    est.add_argument("--interest", default=0)
    est.add_argument("--state-tax-paid", default=0)
    est.add_argument("--property-tax", default=0)
    est.add_argument("--mortgage-interest", default=0)
    est.add_argument("--charitable", default=0)
    est.add_argument("--existing-401k", default=0)
    est.add_argument("--hdhp", action="store_true")
    est.add_argument("--hdhp-family", action="store_true")
    est.add_argument("--method", choices=["regular", "planning"], default="regular")
    est.add_argument("--json", action="store_true")
    est.set_defaults(func=cmd_estimate)

    pay = sub.add_parser("payment", help="price the ways to settle a balance")
    pay.add_argument("balance", type=float, help="positive to pay, negative for a refund")
    pay.add_argument("--year", type=int)
    pay.add_argument("--cannot-pay", action="store_true")
    pay.set_defaults(func=cmd_payment)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
