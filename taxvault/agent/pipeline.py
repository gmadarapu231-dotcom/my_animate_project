"""The agent that turns a pile of documents into a return ready to sign.

What a preparer actually does, in order, and how much of it is mechanical:

  1. Open each document and work out what it is.            mechanical
  2. Read the figures off it.                               mechanical
  3. Check it belongs to this client.                       mechanical
  4. Cross-check the forms against each other.              mechanical
  5. Compute the return.                                    mechanical
  6. Look for what the client could still change.           mechanical
  7. Decide what has to be ASKED rather than assumed.       judgement
  8. Price the work.                                        mechanical
  9. Put the package in front of the client to sign.        judgement
 10. Transmit it.                                           NOT ALLOWED HERE

Steps 1-6 and 8 are what this module automates, and automating them is the
whole commercial case: they are where the hours go, and a machine does them
the same way every time at two in the morning in April.

Step 7 is the one that cannot be automated away, and the reason is not
technical. A 1099-R with code 1 does not say whether the client left their job
at 55. A 1098 does not say whether the HELOC paid for a kitchen or a car. A
W-2 does not say whether the client's partner also worked. Each of those
changes the answer by thousands and no amount of reading the document reveals
it. So the agent's output is not a filed return -- it is a return plus a short,
specific list of questions, each one attached to the figure it would move.

Step 10 is not a gap in this code. Transmitting a return to the IRS requires an
EFIN obtained through e-Services, acceptance testing against the Modernized
e-File system, and a Form 8879 signed by the taxpayer for every return. None of
those are things software can grant itself. `FilingGate` states exactly what is
outstanding, and the agent's terminal state is `ready_for_signature`, never
`filed`.

Every step is recorded with what it did and how long it took, because a client
asking "why does it say I owe $3,200" deserves an answer better than "the
model said so".
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Callable

from taxvault.config import federal, latest_year
from taxvault.engines.capital import CapitalInput
from taxvault.engines.commission import quote_platform_fee
from taxvault.engines.estimate import profile_from_documents, run_estimate, wage_lines_from
from taxvault.engines.fees import quote_from_estimate as quote_fee
from taxvault.engines.mortgage import Loan
from taxvault.engines.retirement import Distribution
from taxvault.enums import EstimateMethod
from taxvault.forms.extract import extract_text, pair_orphan_amounts
from taxvault.forms.f1095 import parse_1095a_text
from taxvault.forms.f1098 import parse_1098_text
from taxvault.forms.f1099 import (
    SIMPLE_FORMS,
    detect_form_kind,
    kinds_present,
    parse_1099b_text,
    parse_1099div_text,
    parse_1099r_text,
    parse_simple_form,
)
from taxvault.forms.identity_match import compare_names
from taxvault.forms.w2 import W2, combine, parse_w2_text
from taxvault.forms.w2_layout import read_w2_layout
from taxvault.money import ZERO, cents, money, positive

#: Step outcomes. `blocked` stops the run; `warn` does not.
OK, WARN, BLOCKED, SKIPPED = "ok", "warn", "blocked", "skipped"

#: The agent's terminal states. There is no `filed`.
READY_FOR_SIGNATURE = "ready_for_signature"
NEEDS_CLIENT_INPUT = "needs_client_input"
NEEDS_PREPARER = "needs_preparer_review"
FAILED = "failed"


# ===========================================================================
# Input and output
# ===========================================================================
@dataclass
class Document:
    """One file as it arrived."""

    filename: str = ""
    content_type: str = ""
    blob: bytes = b""
    #: Set when the caller already knows: skips detection.
    declared_kind: str = ""


@dataclass
class Step:
    name: str
    label: str
    status: str = OK
    detail: str = ""
    findings: list[str] = field(default_factory=list)
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReviewItem:
    """One thing that must be asked, not assumed.

    `moves` is the point of the whole structure: a question with no number
    attached gets skipped, and a question that could change the refund by
    $2,200 does not.
    """

    question: str
    why: str
    field: str = ""
    moves: str = ""
    severity: str = "ask"       # ask | confirm | blocker
    form: str = ""
    #: Set when the client has answered this one. The question stays on the
    #: record -- what was asked and what came back is part of the return --
    #: but it no longer holds the return up.
    answered: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FilingGate:
    """What stands between this return and the IRS.

    Honest by construction: every entry is a real requirement, and the ones
    this software cannot satisfy are marked so nobody plans around them.
    """

    requirements: list[dict[str, Any]] = field(default_factory=list)

    def require(self, key: str, label: str, met: bool, detail: str,
                satisfiable_in_software: bool = True) -> None:
        self.requirements.append({
            "key": key, "label": label, "met": met, "detail": detail,
            "satisfiable_in_software": satisfiable_in_software,
        })

    @property
    def outstanding(self) -> list[dict[str, Any]]:
        return [r for r in self.requirements if not r["met"]]

    @property
    def can_transmit(self) -> bool:
        return not self.outstanding

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirements": self.requirements,
            "outstanding": [r["key"] for r in self.outstanding],
            "can_transmit": self.can_transmit,
        }


@dataclass
class AgentRun:
    tax_year: int
    state: str = FAILED
    steps: list[Step] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)
    review: list[ReviewItem] = field(default_factory=list)
    gate: FilingGate = field(default_factory=FilingGate)
    estimate: dict[str, Any] | None = None
    client_fee: dict[str, Any] | None = None
    platform_fee: dict[str, Any] | None = None
    confidence: float = 0.0
    elapsed_ms: int = 0
    notes: list[str] = field(default_factory=list)
    #: The computed EstimateResult. Not serialised -- `estimate` is its dict
    #: form -- but the fee engine prices off the object.
    _result: Any = field(default=None, repr=False, compare=False)

    @property
    def blockers(self) -> list[ReviewItem]:
        return [item for item in self.review
                if item.severity == "blocker" and not item.answered]

    @property
    def open_questions(self) -> list[ReviewItem]:
        return [item for item in self.review if not item.answered]

    def step(self, name: str, label: str) -> "_StepTimer":
        return _StepTimer(self, name, label)

    def ask(self, item: ReviewItem) -> None:
        self.review.append(item)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tax_year": self.tax_year,
            "state": self.state,
            "confidence": round(self.confidence, 3),
            "elapsed_ms": self.elapsed_ms,
            "steps": [s.to_dict() for s in self.steps],
            "documents": self.documents,
            "review": [item.to_dict() for item in self.review],
            "open_questions": [item.to_dict() for item in self.open_questions],
            "blockers": [item.to_dict() for item in self.blockers],
            "gate": self.gate.to_dict(),
            "estimate": self.estimate,
            "client_fee": self.client_fee,
            "platform_fee": self.platform_fee,
            "notes": self.notes,
        }


class _StepTimer:
    """Records a step whether it succeeds or raises."""

    def __init__(self, run: AgentRun, name: str, label: str):
        self.run, self.step = run, Step(name=name, label=label)
        self._started = 0.0

    def __enter__(self) -> Step:
        self._started = time.perf_counter()
        return self.step

    def __exit__(self, exc_type, exc, _tb) -> bool:
        self.step.duration_ms = int((time.perf_counter() - self._started) * 1000)
        if exc is not None:
            self.step.status = BLOCKED
            self.step.detail = f"{exc_type.__name__}: {exc}"
        self.run.steps.append(self.step)
        return False


# ===========================================================================
# The run
# ===========================================================================
def run_agent(
    documents: list[Document],
    *,
    tax_year: int | None = None,
    taxpayer_name: str = "",
    filing_status: str = "single",
    resident_state: str = "",
    situation: dict[str, Any] | None = None,
    as_of: date | None = None,
    returns_this_period: int = 0,
    answered: set[str] | list[str] | None = None,
    client_reviewed: bool = False,
    client_signed_8879: bool = False,
    preparer_ptin: str = "",
) -> AgentRun:
    """Documents in, a return ready to sign out -- or a list of questions.

    `answered` is the set of `ReviewItem.field` values the client has already
    answered. A second run with those supplied is how a return moves from
    `needs_client_input` to `ready_for_signature`: the questions do not
    disappear, they are marked answered and stop holding the return up.
    """
    started = time.perf_counter()
    year = tax_year or latest_year()
    run = AgentRun(tax_year=year)
    situation = dict(situation or {})

    parsed = _classify_and_extract(run, documents, year)
    _check_identity(run, parsed, taxpayer_name)
    _reconcile(run, parsed, year)
    profile = _assemble(run, parsed, year, filing_status, resident_state, situation)
    _compute(run, profile, parsed, as_of)
    _ask_what_cannot_be_read(run, parsed, profile, set(answered or ()))
    _price(run, parsed, profile, returns_this_period)
    _gate(run, parsed, client_reviewed, client_signed_8879, preparer_ptin)

    run.confidence = _confidence(run, parsed)
    run.state = _state(run)
    run.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return run


# ---------------------------------------------------------------- 1-2. read
def _classify_and_extract(
    run: AgentRun, documents: list[Document], year: int
) -> dict[str, Any]:
    """Work out what each document is and read the figures off it."""
    parsed: dict[str, Any] = {
        "w2": [], "1099_b": [], "1099_div": [], "1099_r": [], "1098": [],
        "1095_a": [], "simple": [], "unreadable": [], "confidences": [],
    }

    with run.step("classify", "Work out what each document is") as step:
        for document in documents:
            extraction = extract_text(
                document.blob, document.content_type, document.filename
            )
            text = extraction.text or ""
            if not extraction.readable or not text.strip():
                # A scan or a photograph. Said plainly rather than guessed at:
                # an invented figure is worse than a blank, because a blank
                # gets asked about and a figure does not.
                parsed["unreadable"].append({
                    "filename": document.filename,
                    "reason": "no text could be read (a scan or photograph needs OCR)",
                })
                continue
            kinds = ([document.declared_kind] if document.declared_kind
                     else kinds_present(text) or [detect_form_kind(text)[0]])
            parsed.setdefault("_texts", []).append(
                {"filename": document.filename, "text": text, "kinds": kinds,
                 "blob": document.blob, "method": extraction.method}
            )
        found = sum(len(t["kinds"]) for t in parsed.get("_texts", []))
        step.detail = (
            f"{len(documents)} file(s): {found} form(s) identified, "
            f"{len(parsed['unreadable'])} unreadable"
        )
        if parsed["unreadable"]:
            step.status = WARN
            step.findings = [
                f"{row['filename']}: {row['reason']}" for row in parsed["unreadable"]
            ]

    with run.step("extract", "Read the figures off each form") as step:
        for entry in parsed.get("_texts", []):
            for kind in entry["kinds"]:
                _extract_one(parsed, kind, entry, year)
        counts = {k: len(v) for k, v in parsed.items()
                  if k in ("w2", "1099_b", "1099_div", "1099_r", "1098", "1095_a")
                  and v}
        for form in parsed["simple"]:
            counts[form.kind] = counts.get(form.kind, 0) + 1
        step.detail = ", ".join(f"{n} x {k.replace('_', '-').upper()}"
                                for k, n in counts.items()) or "nothing read"
        low = [row for row in parsed["confidences"] if row["confidence"] < 0.75]
        if low:
            step.status = WARN
            step.findings = [
                f"{row['kind'].replace('_', '-').upper()} from "
                f"{row['filename']}: read at {row['confidence']:.0%} confidence"
                for row in low
            ]
        if not counts:
            step.status = BLOCKED
            step.detail = "No form could be read, so there is nothing to compute."

    for entry in parsed.get("_texts", []):
        run.documents.append({
            "filename": entry["filename"],
            "kinds": entry["kinds"],
            "method": entry["method"],
        })
    for row in parsed["unreadable"]:
        run.documents.append({
            "filename": row["filename"], "kinds": [], "method": "unreadable",
            "reason": row["reason"],
        })
    parsed.pop("_texts", None)
    return parsed


def _extract_one(parsed: dict[str, Any], kind: str, entry: dict[str, Any],
                 year: int) -> None:
    """One form, one parser. Layout-aware first for a W-2."""
    text, filename = entry["text"], entry["filename"]
    parsers: dict[str, Callable[[str], tuple[Any, float, list[str]]]] = {
        "1099_b": parse_1099b_text,
        "1099_div": parse_1099div_text,
        "1099_r": parse_1099r_text,
        "1098": parse_1098_text,
    }

    if kind == "w2":
        form, confidence, warnings = None, 0.0, []
        if entry["blob"][:5] == b"%PDF-":
            # The layout reader keeps coordinates, which is how box 1 is told
            # apart from the identity box printed beside it.
            try:
                form, confidence, warnings = read_w2_layout(entry["blob"])
            except Exception:
                form = None
        if form is None or confidence < 0.75:
            text_form, text_confidence, text_warnings = parse_w2_text(text)
            if form is None or text_confidence > confidence:
                form, confidence, warnings = text_form, text_confidence, text_warnings
        if confidence < 0.75:
            # Last resort: the amounts came out of the PDF in order but their
            # labels did not. A repair, not a read, so the confidence is cut
            # to match and the caller sees a lower number.
            repaired = pair_orphan_amounts(text)
            if repaired:
                salvaged, salvaged_confidence, salvaged_warnings = parse_w2_text(repaired)
                if salvaged_confidence > confidence:
                    form = salvaged
                    confidence = round(salvaged_confidence * 0.8, 3)
                    warnings = salvaged_warnings + [
                        "The boxes on this W-2 came out of the PDF without their "
                        "labels, so they were matched back by position. Check every "
                        "figure against the form."
                    ]
        if not form.tax_year:
            form.tax_year = year
        parsed["w2"].append(form)
        parsed["confidences"].append(
            {"kind": kind, "filename": filename, "confidence": confidence,
             "warnings": warnings}
        )
        return

    if kind == "1095_a":
        form, confidence, warnings = parse_1095a_text(text)
        if not form.tax_year:
            form.tax_year = year
        parsed["1095_a"].append(form)
        parsed["confidences"].append(
            {"kind": kind, "filename": filename, "confidence": confidence,
             "warnings": warnings}
        )
        return

    if kind in SIMPLE_FORMS:
        form, confidence, warnings = parse_simple_form(text, kind)
        if not form.tax_year:
            form.tax_year = year
        parsed["simple"].append(form)
        parsed["confidences"].append(
            {"kind": kind, "filename": filename, "confidence": confidence,
             "warnings": warnings}
        )
        return

    parser = parsers.get(kind)
    if parser is None:
        return
    form, confidence, warnings = parser(text)
    if not getattr(form, "tax_year", 0):
        form.tax_year = year
    parsed[kind].append(form)
    parsed["confidences"].append(
        {"kind": kind, "filename": filename, "confidence": confidence,
         "warnings": warnings}
    )


# ------------------------------------------------------------ 3. identity
def _check_identity(run: AgentRun, parsed: dict[str, Any], taxpayer_name: str) -> None:
    """Do these forms belong to this client?"""
    with run.step("identity", "Check the forms belong to this client") as step:
        if not taxpayer_name:
            step.status = SKIPPED
            step.detail = "No registered name supplied, so no comparison was made."
            return
        first, _, last = taxpayer_name.strip().partition(" ")
        checks: list[tuple[str, Any]] = []
        for form in parsed["w2"]:
            on_form = " ".join(
                part for part in (form.employee_first_name, form.employee_last_name)
                if part
            ).strip()
            if not on_form:
                continue
            grade = compare_names(
                registered_first=first, registered_last=last.strip(),
                form_first=form.employee_first_name, form_last=form.employee_last_name,
                form_full=on_form,
            )
            checks.append((on_form, grade))
        if not checks:
            step.status = WARN
            step.detail = "No employee name could be read, so nothing was compared."
            run.ask(ReviewItem(
                question=f"Is every form here for {taxpayer_name}?",
                why=("No name could be read off the forms, so the match could not be "
                     "checked. A return filed under the wrong name is rejected by the "
                     "IRS and the refund stops."),
                severity="confirm", form="all",
            ))
            return
        bad = [(name, grade) for name, grade in checks
               if grade.verdict == "mismatch"]
        step.detail = f"{len(checks)} name(s) compared against {taxpayer_name!r}"
        if bad:
            step.status = BLOCKED
            step.findings = [f"{name}: {grade.verdict}" for name, grade in bad]
            for name, grade in bad:
                run.ask(ReviewItem(
                    question=f"The form says {name!r} but the account says "
                             f"{taxpayer_name!r}. Which is right?",
                    why=("The IRS matches the name and SSN against Social Security "
                         "records before anything else. A mismatch rejects the return, "
                         "and a rejected return is not a filed return -- the deadline "
                         "keeps running."),
                    severity="blocker", form="W-2",
                ))


# ----------------------------------------------------------- 4. reconcile
def _reconcile(run: AgentRun, parsed: dict[str, Any], year: int) -> None:
    """Cross-check the forms against each other and against the rate tables."""
    with run.step("reconcile", "Cross-check the forms against each other") as step:
        findings: list[str] = []
        for form in parsed["w2"]:
            for finding in form.validate(year=year):
                findings.append(f"W-2 ({form.employer_name or 'unnamed'}): "
                                f"{finding['message']}")
        for kind in ("1099_div", "1099_r", "1098", "1095_a"):
            for form in parsed[kind]:
                if hasattr(form, "validate"):
                    for finding in form.validate():
                        findings.append(
                            f"{kind.replace('_', '-').upper()}: {finding['message']}"
                        )
        for form in parsed["simple"]:
            for finding in form.validate():
                if finding["severity"] != "info":
                    findings.append(f"{form.label}: {finding['message']}")

        # The same employer twice is nearly always a duplicate upload, and it
        # doubles the wages without looking wrong anywhere.
        employers = [(f.employer_name or "").strip().lower() for f in parsed["w2"]]
        duplicates = {name for name in employers if name and employers.count(name) > 1}
        for name in duplicates:
            run.ask(ReviewItem(
                question=f"There are two W-2s from the same employer ({name}). Is "
                         "that right, or was one uploaded twice?",
                why=("Two W-2s from one employer happens -- a payroll change mid-year "
                     "does it -- but a duplicate upload doubles the wages and the "
                     "estimate with them."),
                severity="blocker", form="W-2",
            ))

        step.detail = f"{len(findings)} cross-check finding(s)"
        step.findings = findings[:12]
        if findings:
            step.status = WARN


# ------------------------------------------------------------ 5. assemble
def _assemble(run: AgentRun, parsed: dict[str, Any], year: int,
              filing_status: str, resident_state: str,
              situation: dict[str, Any]) -> Any:
    """Build the calculation input from every form that was read."""
    with run.step("assemble", "Build the return from the forms") as step:
        profile, warnings = profile_from_documents(
            parsed["w2"], tax_year=year, filing_status=filing_status,
            resident_state=resident_state, extra=situation,
        )

        short = long_ = wash = gain_distributions = ZERO
        dividends = qualified = unrecaptured = collectibles = foreign = ZERO
        for form in parsed["1099_b"]:
            short += form.short_term_gain
            long_ += form.long_term_gain
            wash += form.wash_sale_disallowed
        for form in parsed["1099_div"]:
            dividends += form.ordinary_dividends
            qualified += form.qualified_dividends
            gain_distributions += form.capital_gain_distributions
            unrecaptured += form.unrecaptured_1250
            collectibles += form.collectibles_gain
            foreign += form.foreign_tax_paid
        if parsed["1099_b"] or parsed["1099_div"]:
            profile.capital = CapitalInput(
                short_term=short, long_term=long_,
                capital_gain_distributions=gain_distributions,
                wash_sale_disallowed=wash,
                collectibles_gain=collectibles,
                unrecaptured_1250_gain=unrecaptured,
            )
        if dividends or qualified:
            profile.ordinary_dividends = dividends
            profile.qualified_dividends = qualified
            profile.foreign_tax_paid = profile.foreign_tax_paid or foreign

        if parsed["1099_r"]:
            profile.distributions = [
                Distribution(
                    payer=form.payer,
                    gross=form.gross_distribution,
                    taxable=form.taxable_amount,
                    code=form.distribution_code or "7",
                    federal_withheld=form.federal_withheld,
                    state_withheld=form.state_withheld,
                    roth_basis=form.employee_contributions,
                    age_at_distribution=float(profile.age or 0),
                    plan_kind=form.plan_kind(),
                )
                for form in parsed["1099_r"]
            ]

        # Form 1095-A: the marketplace subsidy to settle up. Folded in before
        # the loans so a cliff warning lands with the rest of the findings.
        if parsed["1095_a"]:
            profile.marketplace = [f.to_coverage() for f in parsed["1095_a"]]

        # The single-box forms each add to one field. They ADD rather than
        # replace, because a client can have three 1099-INTs and the return
        # wants the total, not the last one read.
        for form in parsed["simple"]:
            for name, amount in form.amounts.items():
                if name == "federal_withheld":
                    profile.federal_withheld += amount
                elif hasattr(profile, name):
                    setattr(profile, name, getattr(profile, name) + amount)
        # Contract work is qualified business income unless the client says
        # otherwise, which is the common case for a sole trader.
        if any(f.kind == "1099_nec" for f in parsed["simple"]):
            profile.qbi_income = profile.qbi_income or profile.self_employment_income

        if parsed["1098"]:
            profile.loans = [
                Loan(
                    lender=form.lender,
                    balance=form.outstanding_principal,
                    interest_paid=form.mortgage_interest,
                    points_paid=form.points_paid,
                    mortgage_insurance=form.mortgage_insurance,
                    origination=form.best_origination() or None,
                )
                for form in parsed["1098"]
            ]

        totals = combine(parsed["w2"]) if parsed["w2"] else {}
        step.detail = (
            f"{len(parsed['w2'])} W-2(s) totalling "
            f"{money(totals.get('wages', 0)):,.0f} of wages, "
            f"{len(parsed['1099_r'])} distribution(s), {len(parsed['1098'])} loan(s)"
            + (f", {len(parsed['1095_a'])} marketplace policy(ies)"
               if parsed["1095_a"] else "")
            + (f", {len(parsed['simple'])} other form(s)" if parsed["simple"] else "")
        )
        step.findings = [w["message"] for w in warnings][:8]
        run.notes.extend(w["message"] for w in warnings)
        return profile


# ------------------------------------------------------------- 6. compute
def _compute(run: AgentRun, profile: Any, parsed: dict[str, Any],
             as_of: date | None) -> None:
    with run.step("compute", "Work out the return") as step:
        result = run_estimate(
            profile,
            wage_lines=wage_lines_from(parsed["w2"]),
            method=EstimateMethod.PLANNING.value,
            as_of=as_of,
        )
        # Kept as the object as well as the dict: the fee engine prices off the
        # computed result, and re-deriving it from JSON would be a second
        # source of truth for the same number.
        run._result = result
        run.estimate = result.to_dict()
        step.detail = result.headline()
        federal = result.federal
        step.findings = [
            f"AGI {money(federal.agi):,.0f}",
            f"{federal.deduction_kind} deduction {money(federal.deduction_taken):,.0f}",
            f"total tax {money(federal.total_tax):,.0f}",
        ]
        if federal.warnings:
            step.status = WARN
            step.findings.extend(federal.warnings[:4])


# ----------------------------------------- 7. what the documents cannot say
def _ask_what_cannot_be_read(run: AgentRun, parsed: dict[str, Any],
                             profile: Any, answered: set[str]) -> None:
    """The questions. This is the step that cannot be automated away.

    Each one is attached to the figure it would move, because a question with
    no number next to it gets skipped and a question worth $2,000 does not.
    """
    with run.step("review", "Decide what has to be asked") as step:
        estimate = run.estimate or {}
        federal = estimate.get("federal", {})
        # Questions about the client's POSITION come off the as-filed figures,
        # not the planned ones. The planning engine can contribute its way
        # under the health-subsidy cliff, and if the question is asked of the
        # planned result it never gets asked -- leaving the client over the
        # cliff and unaware, because the advice they have not taken yet is
        # already baked into the number being tested.
        as_filed = (estimate.get("baseline") or {}).get("federal", federal)

        for form in parsed["1099_r"]:
            code = (form.distribution_code or "").upper()
            if "1" in code:
                run.ask(ReviewItem(
                    question="Did you leave that job in or after the year you turned "
                             "55, or does another exception apply?",
                    why=("Box 7 is code 1, so the payer has no exception on file and "
                         "the 10% additional tax is charged. The payer does not know "
                         "about most exceptions -- leaving at 55, disability, medical "
                         "costs, a birth or adoption -- and any of them removes it."),
                    field="distributions[].penalty_exception",
                    moves=f"{money(federal.get('early_withdrawal_penalty', 0)):,.0f}",
                    severity="ask", form="1099-R",
                ))
            if form.taxable_not_determined:
                run.ask(ReviewItem(
                    question="Have you ever made a non-deductible contribution to this "
                             "IRA?",
                    why=("Box 2b says the payer could not work out the taxable amount. "
                         "If any of this money was already taxed going in, part of this "
                         "distribution is your own money coming back and is not taxable "
                         "again. Form 8606 tracks it, and without it you pay twice."),
                    field="ira_basis", severity="ask", form="1099-R",
                ))

        for form in parsed["1098"]:
            run.ask(ReviewItem(
                question=f"Did all of the borrowing from {form.lender or 'this lender'} "
                         "go into the house -- buying it, building it, or improving it?",
                why=("Interest is deductible only on borrowing that bought, built or "
                     "substantially improved the home securing it. A lender does not "
                     "report what the money was spent on, so it has to be asked. Spent "
                     "on a car or a card, none of that interest counts."),
                field="loans[].used_for",
                moves=f"{money((federal.get('mortgage') or {}).get('deductible_interest', 0)):,.0f}",
                severity="confirm", form="1098",
            ))
            if not form.best_origination():
                run.ask(ReviewItem(
                    question="What date was this mortgage taken out?",
                    why=("A loan from on or before 15 December 2017 keeps the older "
                         "$1,000,000 debt ceiling instead of $750,000, for the life of "
                         "the loan. The form does not state the date, and on a large "
                         "mortgage it is worth real money."),
                    field="loans[].origination", severity="ask", form="1098",
                ))

        for form in parsed["1095_a"]:
            if form.benchmark_premium <= 0 and form.annual_premium > 0:
                run.ask(ReviewItem(
                    question="What is the second-lowest-cost silver plan premium "
                             "(column B) on your 1095-A?",
                    why=("Column B is blank, and the health credit is measured against "
                         "it rather than against what you paid. Treating it as zero "
                         "would wipe out the credit entirely. The marketplace's own tax "
                         "tool gives the figure."),
                    field="marketplace[].benchmark_premium",
                    severity="blocker", form="1095-A",
                ))
        cliff = (as_filed.get("premium_tax_credit") or {})
        if cliff.get("over_cliff"):
            at_risk = money(cliff.get("repayment", 0))
            over_by = positive(
                money(cliff.get("household_income", 0))
                - money(cliff.get("poverty_line", 0)) * 4
            )
            run.ask(ReviewItem(
                question="Can any income be moved out of this year, or a deductible "
                         f"contribution of about {over_by:,.0f} be made?",
                why=(f"As your forms stand, household income is "
                     f"{money(cliff.get('income_as_pct_of_fpl', 0)):,.0f}% of the "
                     "federal poverty line -- over 400% -- so the ENTIRE health credit "
                     f"is lost and all {at_risk:,.0f} of the advance is repaid, with no "
                     f"cap. Getting income under the line needs about {over_by:,.0f} of "
                     "deductible contribution, which saves the whole credit: a return "
                     "of roughly "
                     f"{(at_risk / over_by * 100) if over_by else 0:,.0f}% on the money. "
                     "This is the single most valuable thing on this return."),
                field="traditional_ira",
                moves=f"{at_risk:,.0f}",
                severity="confirm", form="1095-A",
            ))

        for form in parsed["simple"]:
            if form.kind == "1099_k" and form.amount("gross_payments") > 0:
                run.ask(ReviewItem(
                    question="How much of the money through that payment app was "
                             "business income, and how much was personal?",
                    why=("A 1099-K reports gross flow, not profit. Selling a personal "
                         "item at a loss or being repaid by a friend shows up here and "
                         "is not income -- but the IRS has the form, so the difference "
                         "has to be explainable."),
                    field="self_employment_income", severity="blocker", form="1099-K",
                ))
            if form.kind == "1099_nec" and form.amount("self_employment_income") > 0:
                run.ask(ReviewItem(
                    question="What did you spend on that work -- equipment, mileage, "
                             "software, a home office?",
                    why=("Contract income is taxed on PROFIT, not on the gross figure "
                         "the payer reported. Expenses come off first and they also "
                         "reduce the 15.3% self-employment tax, so every dollar of "
                         "genuine expense is worth about 30 cents."),
                    field="business_expenses", severity="blocker", form="1099-NEC",
                ))

        if parsed["1099_b"]:
            run.ask(ReviewItem(
                question="Are any of these positions close to their one-year "
                         "anniversary?",
                why=("A position held one day longer than a year is taxed at 0/15/20% "
                     "instead of your ordinary rate. The 1099-B has the acquisition "
                     "dates; whether you are willing to wait is your call, not ours."),
                field="sales.long_term", severity="ask", form="1099-B",
            ))

        if profile.filing_status in ("single", "head_of_household"):
            run.ask(ReviewItem(
                question="Were you married on 31 December?",
                why=("Filing status is decided on the last day of the year and it "
                     "changes the brackets, the standard deduction and most "
                     "phase-outs. It is the single largest thing no document reveals."),
                field="filing_status", severity="confirm", form="",
            ))

        if not parsed["1099_div"] and not parsed["1099_b"]:
            run.ask(ReviewItem(
                question="Do you have a brokerage or investment account?",
                why=("No 1099-B or 1099-DIV was supplied. If you sold anything or "
                     "received dividends, those forms exist and the IRS already has "
                     "them -- a return without them gets a notice."),
                field="sales", severity="ask", form="",
            ))

        for row in parsed["unreadable"]:
            run.ask(ReviewItem(
                question=f"{row['filename']} could not be read. Can you type the "
                         "figures in, or get the original PDF?",
                why=("It is a scan or a photograph, and there is no text in it to "
                     "read. There is no OCR here on purpose: a guessed figure on a tax "
                     "return is worse than a blank one, because a blank gets asked "
                     "about."),
                severity="blocker", form=row["filename"],
            ))

        # Anything the client has already answered is kept on the record --
        # a return has to show what was asked and what came back -- but it
        # stops holding the return up.
        for item in run.review:
            if item.field and item.field in answered:
                item.answered = True

        asks = len([i for i in run.review
                    if i.severity == "ask" and not i.answered])
        confirms = len([i for i in run.review
                        if i.severity == "confirm" and not i.answered])
        blockers = len(run.blockers)
        answered_count = len([i for i in run.review if i.answered])
        step.detail = (
            f"{asks} question(s), {confirms} to confirm, {blockers} blocking"
            + (f", {answered_count} already answered" if answered_count else "")
        )
        step.status = BLOCKED if blockers else (WARN if asks or confirms else OK)


# --------------------------------------------------------------- 8. price
def _price(run: AgentRun, parsed: dict[str, Any], profile: Any,
           returns_this_period: int) -> None:
    with run.step("price", "Price the work") as step:
        # `quote_from_estimate` reads the tier straight off the computed
        # return, so the price follows what the work actually turned out to
        # be rather than what the client said at the start.
        fee = quote_fee(
            run._result,
            w2_count=len(parsed["w2"]),
            planning_session=True,
            profile=profile,
        )
        run.client_fee = fee.to_dict()
        platform = quote_platform_fee(
            preparation_fee=fee.total, returns_this_period=returns_this_period
        )
        run.platform_fee = platform.to_dict()
        step.detail = (
            f"client {money(fee.total):,.2f} ({fee.tier_label}), "
            f"platform {platform.amount:,.2f} ({platform.label})"
        )
        step.findings = [
            "The fee is set by the work the return takes, not by the refund.",
            "Nothing is deducted from the refund: the IRS pays the client directly.",
        ]


# ---------------------------------------------------------------- 9. gate
def _gate(run: AgentRun, parsed: dict[str, Any], client_reviewed: bool,
          client_signed_8879: bool, preparer_ptin: str) -> None:
    """Exactly what stands between this return and the IRS."""
    with run.step("gate", "Check what is needed before filing") as step:
        gate = run.gate
        gate.require(
            "documents_readable", "Every document was readable",
            not parsed["unreadable"],
            "A document nobody could read cannot be on the return."
            if parsed["unreadable"] else "All documents produced figures.",
        )
        gate.require(
            "no_blockers", "No blocking question outstanding",
            not run.blockers,
            f"{len(run.blockers)} question(s) must be answered first."
            if run.blockers else "Nothing blocking.",
        )
        gate.require(
            "client_reviewed", "The client has reviewed the figures",
            client_reviewed,
            "The taxpayer is responsible for the return's contents whoever prepared "
            "it, so they have to see it before it goes.",
        )
        gate.require(
            "form_8879", "Form 8879 signed by the taxpayer",
            client_signed_8879,
            "An e-file authorisation signed by the taxpayer is required before a "
            "preparer may transmit. It is not optional and it cannot be implied "
            "from a click on a different screen.",
        )
        gate.require(
            "preparer_ptin", "A preparer with a PTIN has signed the return",
            bool(preparer_ptin.strip()),
            "Anyone paid to prepare a return must sign it and give their PTIN. "
            "Software is not a preparer.",
        )
        gate.require(
            "efin_transmitter", "An IRS-authorised e-file transmitter",
            False,
            "This system is not an IRS e-file provider. Transmitting needs an EFIN "
            "from the e-Services application and acceptance testing against the "
            "Modernized e-File system. Until then the package is filed through an "
            "authorised transmitter or on paper -- and that is a business "
            "authorisation, not a feature that can be written.",
            satisfiable_in_software=False,
        )
        outstanding = gate.outstanding
        step.detail = (
            "ready to transmit" if gate.can_transmit
            else f"{len(outstanding)} requirement(s) outstanding: "
                 + ", ".join(r["key"] for r in outstanding)
        )
        step.status = OK if gate.can_transmit else WARN
        step.findings = [r["label"] + " -- " + r["detail"] for r in outstanding]


# ===========================================================================
# Scoring
# ===========================================================================
def _confidence(run: AgentRun, parsed: dict[str, Any]) -> float:
    """How much of this return was read cleanly, 0 to 1.

    Deliberately pessimistic: an unreadable document drags the whole run down,
    because a return missing a form is not 90% right.
    """
    scores = [row["confidence"] for row in parsed["confidences"]]
    if not scores:
        return 0.0
    base = sum(scores) / len(scores)
    total_docs = len(scores) + len(parsed["unreadable"])
    coverage = len(scores) / total_docs if total_docs else 0.0
    penalty = 0.15 * len(run.blockers)
    return max(0.0, min(1.0, base * coverage - penalty))


def _state(run: AgentRun) -> str:
    if any(step.status == BLOCKED and step.name in ("extract", "compute")
           for step in run.steps):
        return FAILED
    if run.blockers:
        return NEEDS_CLIENT_INPUT
    if run.confidence < 0.75:
        return NEEDS_PREPARER
    if any(item.severity == "confirm" and not item.answered for item in run.review):
        return NEEDS_CLIENT_INPUT
    return READY_FOR_SIGNATURE
