"""Command line: run the server, estimate a return, inspect the rate tables.

The CLI exists so the engine can be exercised without a browser, which matters
for two people: whoever reviews the rate tables each January, and whoever is
debugging a client's return and wants the figures without the UI in the way.
"""

from __future__ import annotations

import argparse
import textwrap
import json
import sys
from pathlib import Path
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


def cmd_read_w2(args: argparse.Namespace) -> int:
    """Show exactly what is read out of a W-2 PDF, and what is not.

    For the case where an upload comes back wrong: run the file through here
    and the output says which strategy ran, which boxes were located, and --
    with `--dump` -- the raw text and label positions, so the failure can be
    seen rather than guessed at.
    """
    from taxvault.forms.extract import extract_text
    from taxvault.forms.w2 import parse_w2_text
    from taxvault.forms.w2_layout import read_w2_layout

    path = Path(args.file)
    if not path.exists():
        print(f"No such file: {path}")
        return 1
    blob = path.read_bytes()
    kind = "application/pdf" if blob[:5] == b"%PDF-" else ""

    extraction = extract_text(blob, kind, path.name)
    print(f"\nFile        {path.name}  ({len(blob) / 1024:.0f} KB)")
    print(f"Extraction  {extraction.method}  readable={extraction.readable}  "
          f"pages={extraction.pages}")
    for note in extraction.notes:
        print(f"  {note['severity']}: {note['message']}")
    if not extraction.readable:
        print("\nNothing to read. Enter the boxes by hand.\n")
        return 0

    layout, layout_confidence, layout_notes = read_w2_layout(blob)
    flat, flat_confidence, _ = parse_w2_text(extraction.text)
    form, confidence, strategy = (
        (layout, layout_confidence, "layout") if layout_confidence >= flat_confidence
        else (flat, flat_confidence, "text")
    )

    print(f"\nStrategy    {strategy}   (layout {layout_confidence}, text {flat_confidence})")
    print(f"\nNames")
    print(f"  employer            {form.employer_name or '-- NOT FOUND --'}")
    print(f"  employee            {(form.employee_first_name + ' ' + form.employee_last_name).strip() or '-- NOT FOUND --'}")
    print(f"  employer EIN        {form.employer_ein or '-- not found --'}")
    print(f"  tax year            {form.tax_year or '-- not found --'}")

    print("\nBoxes")
    for label, value in [
        ("1  wages", form.wages), ("2  federal withheld", form.federal_withheld),
        ("3  social security wages", form.social_security_wages),
        ("4  social security tax", form.social_security_withheld),
        ("5  medicare wages", form.medicare_wages),
        ("6  medicare tax", form.medicare_withheld),
        ("7  tips", form.social_security_tips),
        ("10 dependent care", form.dependent_care_benefits),
    ]:
        mark = "  " if value else "??"
        print(f"  {mark} {label:<26} {value:>12,.2f}")
    print(f"     {'12 codes':<26} {dict(form.box12) or '-- none found --'}")
    for line in form.states:
        print(f"     {'15-17 ' + line.state:<26} wages {line.state_wages:>12,.2f}  "
              f"tax {line.state_withheld:>10,.2f}")
    if not form.states:
        print(f"  ?? {'15-17 state line':<26} -- NOT FOUND --")

    from taxvault.config import supported_years

    if form.tax_year and form.tax_year not in supported_years():
        nearest = min(supported_years(), key=lambda y: abs(y - form.tax_year))
        print(f"\n  note: {form.tax_year} has no rate table installed, so the "
              f"cross-checks below use {nearest} figures.")
    findings = form.validate(year=form.tax_year or None)
    if findings:
        print("\nCross-checks")
        for finding in findings:
            print(f"  {finding['severity']:<8} box {finding['box']:<3} {finding['message']}")
    for note in layout_notes:
        print(f"  {note['severity']:<8} {note['message']}")

    if args.dump:
        from taxvault.forms.layout import lines_of, words_from_pdf

        print("\n--- lines as the reader sees them (y, x, text) ---")
        for page in words_from_pdf(blob):
            for row in lines_of(page):
                text = " ".join(w.text for w in row)
                print(f"  y={round(row[0].y):>4} x={round(row[0].x):>4}  {text[:120]}")
    else:
        print("\nRun again with --dump to see every line and its position.")
    print()
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


def cmd_newkey(args: argparse.Namespace) -> int:
    """Print a fresh master key. The one value you cannot lose."""
    import base64
    import secrets

    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
    print(key)
    print(file=sys.stderr)
    print("Set this as TAXVAULT_MASTER_KEY.", file=sys.stderr)
    print(file=sys.stderr)
    print("Store it in a secret manager with versioning. Everything sealed with", file=sys.stderr)
    print("it -- every Social Security number, every bank account -- is", file=sys.stderr)
    print("permanently unreadable without it. There is no recovery path and no", file=sys.stderr)
    print("way to re-derive it: it is random. Losing it loses the data.", file=sys.stderr)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Is this environment configured to serve real clients?"""
    from taxvault.db.session import ping
    from taxvault.settings import readiness

    report = readiness()
    reachable, detail = ping()
    width = max(len(c.name) for c in report.checks) + 2

    print(f"environment: {report.environment}")
    print()
    for check in report.checks:
        mark = "ok  " if check.ok else ("WARN" if check.severity == "warning" else "FAIL")
        print(f"  [{mark}] {check.name.ljust(width)} {check.detail}")
        if not check.ok and check.remedy and args.verbose:
            for line in textwrap.wrap(check.remedy, 72):
                print(f"         {line}")
            print()
    mark = "ok  " if reachable else "FAIL"
    print(f"  [{mark}] {'database'.ljust(width)} {detail}")
    print()

    if report.errors or not reachable:
        # Count the database too. Printing "0 blocking problems" above a
        # NOT READY line reads like the tool is broken, and it was: the
        # count only looked at the settings.
        blocking = len(report.errors) + (0 if reachable else 1)
        print(f"NOT READY: {blocking} blocking problem(s).")
        if not reachable:
            print()
            print("The database could not be reached:")
            print(f"  {detail}")
            if "No module named" in detail:
                print()
                print("  That is a missing driver, not a missing database.")
                print("  PostgreSQL needs the deploy extra:")
                print("    pip install -e '.[deploy]'")
                print("  The Docker image installs it already.")
        if not args.verbose and report.errors:
            print("Run with --verbose for what to do about each one.")
        return 1
    if report.warnings:
        print(f"Ready, with {len(report.warnings)} warning(s).")
        return 0
    print("Ready.")
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    """Run the agent against a sandbox scenario, or real files on disk."""
    from taxvault.agent import Document, run_agent, sample_bundle
    from taxvault.agent.sandbox import SCENARIOS, SandboxUnavailable

    if args.list:
        for scenario in SCENARIOS.values():
            print(f"{scenario.key:<14} {scenario.label}")
            for line in textwrap.wrap(scenario.description, 62):
                print(f"               {line}")
            print(f"               expect: {scenario.expect}")
            print()
        return 0

    name = args.taxpayer
    if args.files:
        documents = []
        for path in args.files:
            blob = Path(path).read_bytes()
            kind = "application/pdf" if blob[:5] == b"%PDF-" else "text/plain"
            documents.append(Document(filename=Path(path).name,
                                      content_type=kind, blob=blob))
        status, state = args.filing_status, args.state
        situation = {"age": args.age}
    else:
        try:
            scenario, documents = sample_bundle(args.scenario, year=args.year)
        except SandboxUnavailable as exc:
            print(str(exc), file=sys.stderr)
            return 2
        name = name or scenario.taxpayer_name
        status, state = scenario.filing_status, scenario.resident_state
        situation = dict(scenario.situation)
        print(f"scenario: {scenario.label}")
        print(f"expect:   {scenario.expect}")
        print()

    run = run_agent(
        documents, tax_year=args.year, taxpayer_name=name or "",
        filing_status=status, resident_state=state, situation=situation,
        returns_this_period=args.returns,
        answered={q for q in (args.answered or [])},
        client_reviewed=args.signed, client_signed_8879=args.signed,
        preparer_ptin=args.ptin,
    )

    print(f"{'STATE':<12} {run.state}   confidence {run.confidence:.0%}"
          f"   {run.elapsed_ms}ms")
    print()
    print("steps")
    for step in run.steps:
        print(f"  {step.status:<8} {step.name:<10} {step.detail}")
        if args.verbose:
            for finding in step.findings:
                for line in textwrap.wrap(finding, 68):
                    print(f"                       {line}")
    print()

    print("documents")
    for document in run.documents:
        kinds = ", ".join(k.replace("_", "-").upper() for k in document["kinds"])
        print(f"  {document['filename']:<28} {kinds or document.get('reason', '-')}")
    print()

    if run.estimate:
        federal = run.estimate["federal"]
        totals = run.estimate["totals"]
        # The agent runs in planning mode, so these figures already have the
        # available moves applied. Saying so matters: a client comparing this
        # against their own arithmetic on the raw forms will not match it.
        planned = bool(run.estimate.get("baseline"))
        print("return (with planning applied)" if planned else "return")
        for label, key in (("AGI", "agi"), ("deduction", "deduction_taken"),
                           ("taxable income", "taxable_income")):
            print(f"  {label:<20} {float(federal[key]):>14,.2f}")
        print(f"  {'federal tax':<20} {float(federal['total_tax']):>14,.2f}")
        print(f"  {'federal withheld':<20} {float(federal['total_payments']):>14,.2f}")
        for row in run.estimate.get("states", []):
            print(f"  {(row['name'] + ' tax'):<20} {float(row['tax']):>14,.2f}")
            print(f"  {(row['code'] + ' withheld'):<20} {float(row['withheld']):>14,.2f}")
        # Federal and state together, which is the number a client cares about
        # and the one the headline quotes. Printing only the federal line beside
        # a combined headline reads like an arithmetic error.
        balance = float(totals["total_balance"])
        print(f"  {'refund' if balance < 0 else 'TO PAY':<20} {abs(balance):>14,.2f}"
              "   federal and state together")
        baseline = run.estimate.get("baseline")
        if baseline:
            saving = float(run.estimate["saving_against_baseline"])
            as_is = float(baseline["totals"]["total_balance"])
            print(f"  {'as filed today':<20} {abs(as_is):>14,.2f}"
                  f"   {'refund' if as_is < 0 else 'to pay'}, before any planning")
            if saving:
                print(f"  {'planning saves':<20} {saving:>14,.2f}")
        print()

    open_questions = run.open_questions
    if open_questions:
        print(f"questions ({len(open_questions)})")
        for item in open_questions:
            tag = item.severity.upper()
            print(f"  [{tag}] {item.question}")
            for line in textwrap.wrap(item.why, 66):
                print(f"         {line}")
            if item.moves:
                print(f"         moves: {item.moves}")
            if item.field:
                print(f"         answer with --answered {item.field}")
            print()

    if run.client_fee:
        print("price")
        print(f"  client            {float(run.client_fee['total']):>14,.2f}"
              f"   {run.client_fee['tier_label']}")
        print(f"  platform          {float(run.platform_fee['amount']):>14,.2f}"
              f"   {run.platform_fee['label']}")
        print("  The fee is set by the work the return takes, not by the refund.")
        print()

    print("before this can be filed")
    for requirement in run.gate.requirements:
        mark = "done" if requirement["met"] else "OPEN"
        note = "" if requirement["satisfiable_in_software"] else "   <- not software"
        print(f"  [{mark}] {requirement['label']}{note}")
    print()
    if not run.gate.can_transmit:
        blocked = [r for r in run.gate.outstanding
                   if not r["satisfiable_in_software"]]
        if blocked:
            print("This return cannot be transmitted from here:")
            for requirement in blocked:
                for line in textwrap.wrap(requirement["detail"], 72):
                    print(f"  {line}")
    return 0


def cmd_walkthrough(args: argparse.Namespace) -> int:
    """One client start to finish, including the money reaching the practice."""
    from taxvault.agent.walkthrough import run

    return run(tax_year=args.year, scenario=args.scenario,
               practice=args.practice, zelle_address=args.zelle)


def cmd_verify(args: argparse.Namespace) -> int:
    """Prove this installation works, on this machine, with this config."""
    import warnings

    warnings.filterwarnings("ignore", category=DeprecationWarning)
    from taxvault.verify import run

    return run(verbose=args.verbose, trace=args.trace)


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

    read = sub.add_parser("read-w2", help="show what is read out of a W-2 PDF")
    read.add_argument("file", help="the PDF to read")
    read.add_argument("--dump", action="store_true",
                      help="also print every line with its position on the page")
    read.set_defaults(func=cmd_read_w2)

    ver = sub.add_parser(
        "verify", help="prove this installation works end to end")
    ver.add_argument("-v", "--verbose", action="store_true")
    ver.add_argument("--trace", action="store_true",
                     help="show the full error for a failing check")
    ver.set_defaults(func=cmd_verify)

    walk = sub.add_parser(
        "walkthrough",
        help="one client end to end, including the fee reaching your account")
    walk.add_argument("--year", type=int, default=2026)
    walk.add_argument("--scenario", default="investor")
    walk.add_argument("--practice", default="Madarapu Tax Associates")
    walk.add_argument("--zelle", default="billing@madarapu-tax.example")
    walk.set_defaults(func=cmd_walkthrough)

    agent = sub.add_parser(
        "agent", help="run the agent on a sandbox scenario or your own files")
    agent.add_argument("scenario", nargs="?", default="investor",
                       help="sandbox scenario (see --list)")
    agent.add_argument("--list", action="store_true", help="list the scenarios")
    agent.add_argument("--files", nargs="*", help="run on real files instead")
    agent.add_argument("--year", type=int, default=None, help="tax year")
    agent.add_argument("--taxpayer", default="", help="the registered name")
    agent.add_argument("--filing-status", default="single")
    agent.add_argument("--state", default="", help="resident state code")
    agent.add_argument("--age", type=int, default=40)
    agent.add_argument("--returns", type=int, default=0,
                       help="returns filed this period, for volume pricing")
    agent.add_argument("--answered", nargs="*", default=[],
                       help="question fields the client has answered")
    agent.add_argument("--signed", action="store_true",
                       help="treat the client as having reviewed and signed Form 8879")
    agent.add_argument("--ptin", default="", help="the preparer's PTIN")
    agent.add_argument("-v", "--verbose", action="store_true")
    agent.set_defaults(func=cmd_agent)

    sub.add_parser("newkey", help="generate a master encryption key").set_defaults(
        func=cmd_newkey)

    check = sub.add_parser(
        "check", help="is this environment safe to serve real clients?")
    check.add_argument("-v", "--verbose", action="store_true",
                       help="print what to do about each problem")
    check.set_defaults(func=cmd_check)

    pay = sub.add_parser("payment", help="price the ways to settle a balance")
    pay.add_argument("balance", type=float, help="positive to pay, negative for a refund")
    pay.add_argument("--year", type=int)
    pay.add_argument("--cannot-pay", action="store_true")
    pay.set_defaults(func=cmd_payment)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
