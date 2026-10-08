"""`taxvault verify` -- does this installation actually work?

Not the test suite. The test suite needs pytest and the dev extras and proves
the code is right; this proves THIS INSTALLATION is right, on the machine it
is running on, with the configuration it has. Run it after every deploy and
after every upgrade.

The difference matters. A deployment can pass every unit test on a build
machine and still be wrong: a parameter file missing from the wheel, a
dependency that silently did not install, a master key that changed, a
database that migrated half way. Each check below exercises one of those
through the real code path, and every expected figure was worked out by hand
from the published rate schedules rather than captured from a previous run.

Exit code 0 means every check passed. Anything else names what failed and what
to do about it.
"""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

#: Width of the check name column, so the output lines up.
_NAME = 46


@dataclass
class Check:
    name: str
    group: str
    passed: bool = False
    detail: str = ""
    remedy: str = ""
    skipped: bool = False
    ms: int = 0


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and not c.skipped]

    @property
    def passed(self) -> list[Check]:
        return [c for c in self.checks if c.passed]

    @property
    def skipped(self) -> list[Check]:
        return [c for c in self.checks if c.skipped]

    @property
    def ok(self) -> bool:
        return not self.failed


def D(value: object) -> Decimal:
    return Decimal(str(value))


class _Runner:
    """Collects checks, catching anything a check throws."""

    def __init__(self) -> None:
        self.report = Report()
        self.group = ""

    def section(self, name: str) -> None:
        self.group = name

    def check(self, name: str, remedy: str = "") -> Callable:
        def wrap(fn: Callable[[], str]) -> None:
            entry = Check(name=name, group=self.group, remedy=remedy)
            started = time.perf_counter()
            try:
                entry.detail = fn() or "ok"
                entry.passed = True
            except _Skip as skip:
                entry.skipped = True
                entry.detail = str(skip)
            except AssertionError as exc:
                entry.detail = str(exc) or "the expected value did not match"
            except Exception as exc:
                entry.detail = f"{type(exc).__name__}: {exc}"
                entry.remedy = entry.remedy or _TRACE_HINT
            entry.ms = int((time.perf_counter() - started) * 1000)
            self.report.checks.append(entry)
        return wrap


class _Skip(Exception):
    """This check cannot run here, and that is not a failure."""


_TRACE_HINT = "Run with --trace to see the full error."


# ===========================================================================
# The checks
# ===========================================================================
def run(*, verbose: bool = False, trace: bool = False) -> int:
    runner = _Runner()
    _install(runner)
    _parameters(runner)
    _arithmetic(runner)
    _encryption(runner)
    _forms(runner)
    _agent(runner)
    _documents(runner)
    _money(runner)
    _automation(runner)
    _environment(runner)
    return _print(runner.report, verbose=verbose, trace=trace)


# ------------------------------------------------------------ 1. install
def _install(r: _Runner) -> None:
    r.section("Installation")

    @r.check("Python is 3.11 or newer", "Install Python 3.11+ and reinstall.")
    def _() -> str:
        import sys

        assert sys.version_info >= (3, 11), f"this is {sys.version.split()[0]}"
        return f"Python {sys.version.split()[0]}"

    @r.check("Every required dependency imports",
             "Run `pip install -e .` -- a `git pull` alone does not install "
             "new dependencies, and a missing one fails at the first upload.")
    def _() -> str:
        import importlib

        needed = {
            "fastapi": "the API", "sqlalchemy": "the database",
            "pydantic": "request validation", "yaml": "the parameter files",
            "cryptography": "sealing Social Security numbers",
            "pypdf": "reading a PDF W-2", "multipart": "file uploads",
        }
        missing = []
        for module, why in needed.items():
            try:
                importlib.import_module(module)
            except ImportError:
                missing.append(f"{module} ({why})")
        assert not missing, "missing: " + ", ".join(missing)
        return f"{len(needed)} dependencies present"

    @r.check("The package's data files shipped with it",
             "The parameter YAML files are package data. If they are absent "
             "the wheel was built without them: reinstall with `pip install -e .`.")
    def _() -> str:
        from taxvault.config import CONFIG_DIR, supported_years

        years = supported_years()
        assert years, f"no federal parameter files found under {CONFIG_DIR}"
        for name in ("fees.yaml", "payments.yaml", "resources.yaml",
                     "commission.yaml"):
            assert (CONFIG_DIR / name).exists(), f"{name} is missing"
        return f"tax years {years}, plus the price list and resources"


# --------------------------------------------------------- 2. parameters
def _parameters(r: _Runner) -> None:
    r.section("Tax parameters")

    @r.check("Every tax year loads and merges the shared layer")
    def _() -> str:
        from taxvault.config import federal, supported_years

        out = []
        for year in supported_years():
            params = federal(year)
            # From the year's own file.
            assert params.amount("standard_deduction", "single") > 0, year
            # From shared.yaml, merged underneath.
            assert params.amount("capital_losses", "ordinary_offset_cap") == D(3000)
            assert params.get("retirement_distributions", "uniform_lifetime_table")
            out.append(str(year))
        return ", ".join(out)

    @r.check("All 51 jurisdictions are present")
    def _() -> str:
        from taxvault.config import states

        table = states()
        codes = table.codes()
        assert len(codes) == 51, f"found {len(codes)}, expected 51 (50 states + DC)"
        no_tax = table.no_tax_states()
        assert len(no_tax) == 9, f"{len(no_tax)} no-tax states, expected 9"
        return f"{len(codes)} jurisdictions, {len(no_tax)} with no income tax"

    @r.check("The year being filed is not a year nobody can file")
    def _() -> str:
        from datetime import date

        from taxvault.config import latest_year, supported_years

        this_year = date.today().year
        finished = [y for y in supported_years() if y < this_year]
        current = finished[-1] if finished else latest_year()
        assert current < this_year, (
            f"the filing year resolved to {current}, which has not ended"
        )
        return f"filing {current}; {latest_year()} available for planning"


# --------------------------------------------------------- 3. arithmetic
def _arithmetic(r: _Runner) -> None:
    r.section("Arithmetic")

    @r.check("A single wage earner, 2025, computed by hand")
    def _() -> str:
        # $95,000 wages less the $15,750 standard deduction is $79,250.
        #   11,925 @ 10% =  1,192.50
        #   36,550 @ 12% =  4,386.00
        #   30,775 @ 22% =  6,770.50
        #                  ---------
        #                  12,349.00
        from taxvault.engines.federal import TaxProfile, compute_federal

        result = compute_federal(TaxProfile(tax_year=2025, wages=D(95000)))
        assert result.taxable_income == D("79250.00"), result.taxable_income
        assert result.total_tax == D("12349.00"), result.total_tax
        return f"tax {result.total_tax} on {result.taxable_income} taxable"

    @r.check("Long-term gain is stacked above ordinary income, not beside it")
    def _() -> str:
        # $50,000 wages and $60,000 long-term gain, single, 2026.
        # Ordinary taxable is 33,900, so the 0% band (to 49,450) has 15,550 of
        # room; the remaining 44,450 of gain is taxed at 15% = 6,667.50.
        from taxvault.engines.federal import TaxProfile, compute_federal

        result = compute_federal(TaxProfile(
            tax_year=2026, wages=D(50000), long_term_gains=D(60000)))
        assert result.preferential_tax == D("6667.50"), result.preferential_tax
        return f"preferential tax {result.preferential_tax}"

    @r.check("A capital loss nets by character and carries forward")
    def _() -> str:
        from taxvault.config import federal
        from taxvault.engines.capital import CapitalInput, compute_capital

        # A short-term loss against a long-term gain: the survivor stays long.
        survived = compute_capital(
            CapitalInput(short_term=D(-4000), long_term=D(10000)), federal(2026))
        assert survived.preferential_component == D("6000.00")
        assert survived.ordinary_component == D("0")
        # A net loss is capped at 3,000 and spends it against short-term first.
        netted = compute_capital(
            CapitalInput(short_term=D(-12000), long_term=D(-3000)), federal(2026))
        assert netted.loss_deduction == D("3000.00")
        assert netted.carryforward_short == D("9000.00")
        assert netted.carryforward_long == D("3000.00")
        return "survivor keeps its character; 3,000 cap; 12,000 carried"

    @r.check("The mortgage debt ceiling prorates rather than disallows")
    def _() -> str:
        # $900,000 of post-2017 debt against a $750,000 ceiling: 83.33% of the
        # interest. The same balance from 2016 keeps the $1m ceiling.
        from taxvault.config import federal
        from taxvault.engines.mortgage import Loan, compute_mortgage

        new = compute_mortgage(
            [Loan(balance=D(900000), interest_paid=D(36000),
                  origination="2022-01-10")], federal(2026))
        assert new.deductible_interest == D("30000.00"), new.deductible_interest
        old = compute_mortgage(
            [Loan(balance=D(900000), interest_paid=D(36000),
                  origination="2016-04-01")], federal(2026))
        assert old.deductible_interest == D("36000.00")
        return "750k ceiling prorates to 30,000; a 2016 loan keeps all 36,000"

    @r.check("The 401(k) limits and the early-withdrawal tax are right")
    def _() -> str:
        from taxvault.config import federal
        from taxvault.engines.retirement import (
            ContributionPlan,
            Distribution,
            check_contributions,
            compute_distributions,
            compute_rmd,
        )

        params = federal(2026)
        # 2026: 24,500 base, 11,250 catch-up at ages 60-63.
        at_61 = check_contributions(ContributionPlan(age=61), params)
        assert at_61.total_elective_limit == D("35750.00"), at_61.total_elective_limit
        # 10% on an early distribution, capped exception at 5,000.
        plain = compute_distributions(
            [Distribution(gross=D(20000), code="1", age_at_distribution=45)], params)
        assert plain.penalty == D("2000.00")
        capped = compute_distributions(
            [Distribution(gross=D(20000), code="1", age_at_distribution=33,
                          penalty_exception="birth_or_adoption")], params)
        assert capped.penalty == D("1500.00")
        # RMD at 73 in 2026: 500,000 / 26.5.
        rmd = compute_rmd(params, birth_year=1953, prior_year_balance=D(500000))
        assert rmd.amount == D("18867.92"), rmd.amount
        return "limit 35,750 at 61; 10% charged; RMD 18,867.92"

    @r.check("The health subsidy cliff bites on a dollar, not a rounding")
    def _() -> str:
        from taxvault.config import federal
        from taxvault.engines.health import (
            MarketplaceCoverage,
            compute_premium_tax_credit,
            poverty_line,
        )

        params = federal(2026)
        policy = [MarketplaceCoverage(annual_premium=D(14400),
                                      benchmark_premium=D(15600),
                                      advance_credit=D(11000))]
        line = poverty_line(params, household_size=3)
        under = compute_premium_tax_credit(
            policy, params, household_income=line * 4 - D(1), household_size=3,
            filing_status="married_jointly")
        over = compute_premium_tax_credit(
            policy, params, household_income=line * 4 + D(1), household_size=3,
            filing_status="married_jointly")
        assert under.eligible is True, "a dollar under the line lost the credit"
        assert over.eligible is False, "a dollar over the line kept the credit"
        assert over.repayment == D("11000.00"), over.repayment
        return f"400% of the line is {line * 4}; one dollar over repays 11,000"

    @r.check("A no-tax state is no tax, and a taxing one is not")
    def _() -> str:
        from taxvault.engines.state import compute_state

        texas = compute_state("TX", state_income=D(95000), year=2025)
        assert texas.tax == D("0"), texas.tax
        california = compute_state("CA", state_income=D(95000), year=2025)
        assert california.tax > D(0), "California computed no tax on 95,000"
        return f"Texas 0.00, California {california.tax}"


# --------------------------------------------------------- 4. encryption
def _encryption(r: _Runner) -> None:
    r.section("Encryption")

    @r.check("A Social Security number round-trips through the seal",
             "If this fails the master key changed. Anything stored under the "
             "old key is unreadable: restore the key, do not re-key.")
    def _() -> str:
        from taxvault.crypto import open_ssn, redact, seal_ssn

        ciphertext, index, last4, kind = seal_ssn("123-45-6789", context="verify:1")
        reopened = open_ssn(ciphertext, context="verify:1")
        assert reopened.digits == "123456789", reopened.masked
        assert last4 == "6789", last4
        assert kind == "ssn", kind
        # Neither the ciphertext nor the blind index may contain the number.
        assert "123456789" not in ciphertext and "123456789" not in index
        # The blind index is deterministic, so a search can find it again...
        again, index_again, _, _ = seal_ssn("123-45-6789", context="verify:2")
        assert index_again == index, "the blind index is not searchable"
        # ...while the ciphertext is not, so two rows do not look alike.
        assert again != ciphertext, "the same SSN sealed to identical ciphertext"
        # And the log filter strips it.
        assert "123-45-6789" not in redact("client ssn is 123-45-6789")
        return "sealed, reopened, searchable by index, redacted from logs"

    @r.check("An invalid Social Security number is refused")
    def _() -> str:
        from taxvault.crypto import normalise_ssn

        for bad in ("000-12-3456", "666-12-3456", "900-12-3456",
                    "123-00-4567", "123-45-0000", "12345"):
            try:
                normalise_ssn(bad)
            except Exception:
                continue
            raise AssertionError(f"{bad} was accepted and should not have been")
        return "6 invalid patterns refused"

    @r.check("Ciphertext is bound to its row")
    def _() -> str:
        # The context is authenticated data: the same ciphertext must not open
        # against a different row, or a swapped database row reads as valid.
        from taxvault.crypto import open_ssn, seal_ssn

        ciphertext, _, _, _ = seal_ssn("123-45-6789", context="taxpayer:1")
        try:
            open_ssn(ciphertext, context="taxpayer:2")
        except Exception:
            return "a row's ciphertext will not open against another row"
        raise AssertionError("ciphertext opened under the wrong context")


# -------------------------------------------------------------- 5. forms
def _forms(r: _Runner) -> None:
    r.section("Reading forms")

    @r.check("A W-2 PDF is read box by box",
             "Needs reportlab: `pip install reportlab`.")
    def _() -> str:
        from taxvault.forms.extract import extract_text

        pdf = _sample_w2()
        extraction = extract_text(pdf, "application/pdf", "w2.pdf")
        assert extraction.readable, "no text came out of the PDF"
        from taxvault.forms.w2_layout import read_w2_layout

        form, confidence, _ = read_w2_layout(pdf)
        assert form.employer_name, "no employer name was read"
        assert form.wages == D("68000.00"), form.wages
        assert form.federal_withheld == D("7400.00"), form.federal_withheld
        assert confidence >= 0.75, f"read at only {confidence:.0%}"
        return (f"{form.employer_name}: box 1 {form.wages}, "
                f"read at {confidence:.0%}")

    @r.check("Every form kind is recognised")
    def _() -> str:
        from taxvault.api.routers.documents import _PARSERS

        expected = {
            "1095_a", "1098", "1098_e", "1098_t", "1099_b", "1099_div",
            "1099_g", "1099_int", "1099_k", "1099_misc", "1099_nec",
            "1099_r", "ssa_1099",
        }
        missing = expected - set(_PARSERS)
        assert not missing, f"no parser for {sorted(missing)}"
        return f"{len(_PARSERS)} form kinds, plus the W-2"

    @r.check("A scan with no text is refused, not guessed at")
    def _() -> str:
        from taxvault.forms.extract import extract_text

        scan = _sample_scan()
        extraction = extract_text(scan, "application/pdf", "photo.pdf")
        assert not (extraction.text or "").strip(), (
            "text came out of a document that has none"
        )
        return "reported as unreadable rather than estimated"


# -------------------------------------------------------------- 6. agent
def _agent(r: _Runner) -> None:
    r.section("The agent")

    @r.check("Every sandbox scenario runs end to end",
             "Needs reportlab: `pip install reportlab`.")
    def _() -> str:
        from taxvault.agent import run_agent, sample_bundle
        from taxvault.agent.pipeline import FAILED
        from taxvault.agent.sandbox import SCENARIOS

        lines = []
        for key in SCENARIOS:
            case, documents = sample_bundle(key, year=2026)
            run = run_agent(
                documents, tax_year=2026, taxpayer_name=case.taxpayer_name,
                filing_status=case.filing_status,
                resident_state=case.resident_state,
                situation=dict(case.situation),
            )
            assert run.state != FAILED, f"{key} failed: {run.steps[-1].detail}"
            assert run.estimate is not None, f"{key} produced no return"
            lines.append(f"{key} {run.confidence:.0%}")
        return ", ".join(lines)

    @r.check("No code path can transmit a return to the IRS",
             "This SHOULD fail only if a transmitter was added. Filing needs "
             "an EFIN and MeF acceptance testing.")
    def _() -> str:
        from taxvault.agent import run_agent, sample_bundle
        from taxvault.agent.sandbox import SCENARIOS

        for key in SCENARIOS:
            case, documents = sample_bundle(key, year=2026)
            run = run_agent(
                documents, tax_year=2026, taxpayer_name=case.taxpayer_name,
                filing_status=case.filing_status,
                resident_state=case.resident_state,
                situation=dict(case.situation),
                answered={"loans[].used_for", "sales.long_term", "filing_status",
                          "sales", "distributions[].penalty_exception",
                          "ira_basis", "business_expenses",
                          "self_employment_income", "traditional_ira",
                          "marketplace[].benchmark_premium"},
                client_reviewed=True, client_signed_8879=True,
                preparer_ptin="P01234567",
            )
            assert run.gate.can_transmit is False, f"{key} believed it could file"
        return "the e-file gate holds shut on every path"

    @r.check("A name mismatch stops the return")
    def _() -> str:
        from taxvault.agent import Document, run_agent

        run = run_agent(
            [Document(filename="w2.pdf", content_type="application/pdf",
                      blob=_sample_w2())],
            tax_year=2026, taxpayer_name="Someone Else Entirely",
        )
        assert run.blockers, "a mismatched name was allowed through"
        return "flagged as blocking before anything else happens"


# ------------------------------------------------------ 7. what they keep
def _documents(r: _Runner) -> None:
    """The estimate as something the client can hold.

    An installation that computes a perfect return and cannot hand it to
    anybody has no deliverable, so this is an acceptance check and not a
    nicety.
    """
    r.section("What the client takes away")

    @r.check("The estimate comes out as a document",
             "Drawing the PDF needs reportlab: `pip install reportlab`.")
    def _() -> str:
        from taxvault.reports.estimate import (
            PdfUnavailable,
            build_estimate_document,
            render_html,
            render_pdf,
        )

        document = build_estimate_document(
            _SAMPLE_ESTIMATE, client_name="Verify Sample", ssn_last4="6789",
            estimate_id=1,
        )
        page = render_html(document)
        assert "Estimated tax summary" in page
        assert "$1,292.64" in page, "the headline figure is missing from the page"
        try:
            pdf = render_pdf(document)
        except PdfUnavailable as exc:
            raise _Skip(str(exc)) from exc
        assert pdf.startswith(b"%PDF-"), "that is not a PDF"
        return f"{len(pdf):,} bytes of PDF and {len(page):,} of HTML, same figures"

    @r.check("Every page says it is not a filed return",
             "A client who files this away believing it was sent does not pay.")
    def _() -> str:
        import io

        from taxvault.reports.estimate import (
            PdfUnavailable,
            build_estimate_document,
            render_pdf,
        )

        document = build_estimate_document(_SAMPLE_ESTIMATE, client_name="Verify Sample")
        try:
            blob = render_pdf(document)
        except PdfUnavailable as exc:
            raise _Skip(str(exc)) from exc
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover
            raise _Skip("pypdf is needed to read the page back") from exc
        pages = PdfReader(io.BytesIO(blob)).pages
        for number, page in enumerate(pages, 1):
            assert "not a filed tax return" in page.extract_text(), (
                f"page {number} does not say what this document is"
            )
        return f"all {len(pages)} page(s) carry the disclaimer"


#: Enough of a real payload to render a document from, with figures checked by
#: hand: 93,500 of wages, the 2025 single standard deduction, 12,019 of tax
#: against 11,200 withheld, and California taking 4,573.64 against 4,100.
_SAMPLE_ESTIMATE = {
    "tax_year": 2025, "method": "regular", "filing_status": "single",
    "resident_state": "CA",
    "federal": {
        "lines": [{"form": "1040", "label": "Wages, salaries, tips (Box 1)",
                   "amount": "93500.00"},
                  {"form": "1040", "label": "Total income", "amount": "93500.00"}],
        "total_income": "93500.00", "agi": "93500.00", "adjustments": "0.00",
        "deduction_kind": "standard", "deduction_taken": "15750.00",
        "standard_deduction": "15750.00", "itemised_deduction": "0.00",
        "taxable_income": "77750.00", "ordinary_tax": "12019.00",
        "total_tax": "12019.00", "total_payments": "11200.00",
        "effective_rate": 0.1285, "marginal_rate": 0.22,
        "notes": [], "warnings": [],
    },
    "states": [{"code": "CA", "name": "California", "total_tax": "4573.64",
                "withheld": "4100.00", "balance": "473.64", "lines": [], "notes": []}],
    "totals": {"federal_tax": "12019.00", "state_tax": "4573.64",
               "federal_balance": "819.00", "state_balance": "473.64",
               "total_balance": "1292.64"},
    "strategies": [], "applied": [],
}


# -------------------------------------------------------------- 8. money
def _money(r: _Runner) -> None:
    r.section("Getting paid")

    @r.check("A fee is never quoted at nothing by accident")
    def _() -> str:
        from taxvault.engines.fees import quote

        unknown = quote(w2_count=1, planning_session=True)
        assert unknown.total > D(0), (
            "a quote with no income information came out free"
        )
        priced = quote(agi=D(199680), w2_count=1, planning_session=True,
                       capital_gains=D(21700), investment_income=D(6480))
        assert priced.total > D(400), priced.total
        # The deliberate pro-bono case still works.
        assert quote(agi=D(18000), w2_count=1, dependents=1).total == D(0)
        return (f"unknown income {unknown.total}; 199,680 -> {priced.total}; "
                "low income free")

    @r.check("A refund-percentage fee is refused with the citation",
             "If this stops failing, someone has made a prohibited fee model "
             "available. Circular 230 section 10.27 forbids it.")
    def _() -> str:
        from taxvault.engines.commission import (
            ProhibitedFeeModel,
            quote_platform_fee,
            refund_handling,
        )

        try:
            quote_platform_fee(model="refund_percentage", refund=D(4000))
        except ProhibitedFeeModel as exc:
            assert "10.27" in str(exc), "the refusal lost its citation"
        else:
            raise AssertionError("a percentage-of-refund fee was computed")
        assert refund_handling()["allowed"] is False
        return "refused, and a fee cannot be taken out of a refund either"

    @r.check("A payment reference survives a mistyped digit")
    def _() -> str:
        from taxvault.engines.revenue import (
            find_reference,
            new_reference,
            reference_is_valid,
        )

        reference = new_reference(tax_year=2026)
        assert reference_is_valid(reference)
        wrong = reference[:-1] + ("A" if reference[-1] != "A" else "B")
        assert not reference_is_valid(wrong), (
            "a mistyped reference validated, so it would credit another client"
        )
        line = f"ZELLE FROM A CLIENT ON 10/08 MEMO {reference} CONF 1"
        assert find_reference(line) == reference
        return f"{reference} validates; one wrong character does not"

    @r.check("Money reaches the practice's account only on confirmation")
    def _() -> str:
        return _money_round_trip()

    @r.check("A receipt tells the client, once, and says what matters")
    def _() -> str:
        return _receipt_check()


# --------------------------------------------------------- 9. automation
def _automation(r: _Runner) -> None:
    r.section("Automation")

    @r.check("The jobs run, and running twice does not duplicate")
    def _() -> str:
        return _automation_check()

    @r.check("Confirming money is never automatic",
             "If this stops failing, something has been allowed to book "
             "money on a string match.")
    def _() -> str:
        import inspect

        from taxvault.agent import automation

        source = inspect.getsource(automation.reconcile)
        assert "auto_confirm=False" in source, (
            "the reconciliation job no longer forces auto_confirm off"
        )
        assert "auto_confirm=True" not in source
        return "the reconciliation job proposes matches and books none"


# -------------------------------------------------------- 10. environment
def _environment(r: _Runner) -> None:
    r.section("This environment")

    @r.check("The database is reachable",
             "Check TAXVAULT_DATABASE_URL and that the server can reach it. "
             "A 'No module named psycopg' is a missing DRIVER, not a missing "
             "database: run `pip install -e '.[deploy]'`.")
    def _() -> str:
        from taxvault.db.session import database_url, ping

        reachable, detail = ping()
        if not reachable and "No module named" in detail:
            raise AssertionError(
                f"{detail} -- that is the database driver, not the database"
            )
        assert reachable, detail
        return f"{database_url().split('://', 1)[0]}: {detail}"

    @r.check("The schema is current",
             "Run `alembic upgrade head` BEFORE starting the new version.")
    def _() -> str:
        try:
            from alembic.config import Config
            from alembic.runtime.migration import MigrationContext
            from alembic.script import ScriptDirectory
        except ImportError:
            raise _Skip("alembic is not installed (fine for a local run)")

        from pathlib import Path

        from taxvault.db.session import get_engine

        root = Path(__file__).resolve().parent.parent
        ini = root / "alembic.ini"
        if not ini.exists():
            raise _Skip("no alembic.ini beside the package")
        config = Config(str(ini))
        config.set_main_option("script_location", str(root / "migrations"))
        head = ScriptDirectory.from_config(config).get_current_head()
        with get_engine().connect() as connection:
            applied = MigrationContext.configure(connection).get_current_revision()
        if applied is None:
            raise _Skip(
                "no migration has been applied; this database was created by "
                "init_db(). Fine locally, not for production."
            )
        assert applied == head, (
            f"the database is at {applied} but the code expects {head}"
        )
        return f"at {head}"

    @r.check("The settings are right for this environment",
             "Run `taxvault check --verbose` for what each one needs.")
    def _() -> str:
        from taxvault.settings import environment, readiness

        report = readiness()
        assert report.ok, (
            f"{len(report.errors)} blocking setting(s): "
            + ", ".join(c.name for c in report.errors)
        )
        warned = len(report.warnings)
        return (f"{environment()}"
                + (f", {warned} warning(s)" if warned else ", no warnings"))


# ===========================================================================
# Helpers
# ===========================================================================
def _sample_w2() -> bytes:
    try:
        from taxvault.agent.sandbox import w2_pdf
    except ImportError:  # pragma: no cover - defensive
        raise _Skip("the sandbox is not available")
    from taxvault.agent.sandbox import SandboxUnavailable

    try:
        return w2_pdf(
            employer="Fresno Produce Co", ein="94-1234567", first="Alex",
            last="Rivera", year=2026, wages=68000, withheld=7400,
            deferral=4000, state="CA", state_withheld=2650,
        )
    except SandboxUnavailable as exc:
        raise _Skip(str(exc))


def _sample_scan() -> bytes:
    from taxvault.agent.sandbox import SandboxUnavailable, scanned_pdf

    try:
        return scanned_pdf()
    except SandboxUnavailable as exc:
        raise _Skip(str(exc))


def _money_round_trip() -> str:
    """Request, declare, confirm -- against a throwaway database."""
    import os
    import tempfile

    from taxvault.db.session import reset_engine

    previous = {
        key: os.environ.get(key)
        for key in ("TAXVAULT_HOME", "TAXVAULT_DATABASE_URL", "TAXVAULT_ENV")
    }
    workspace = tempfile.mkdtemp(prefix="taxvault-verify-")
    try:
        os.environ.update(
            TAXVAULT_HOME=workspace,
            TAXVAULT_DATABASE_URL=f"sqlite:///{workspace}/verify.db",
        )
        os.environ.pop("TAXVAULT_ENV", None)
        reset_engine()

        from taxvault.db.models import Account, FeeQuoteRecord, Taxpayer
        from taxvault.db.session import init_db, new_session
        from taxvault.engines.remittance import BUCKET_FEE, record_funds
        from taxvault.engines.revenue import (
            confirm_payment,
            confirmed_total,
            declare_payment,
            firm_account,
            request_fee_payment,
        )

        init_db()
        session = new_session()
        try:
            account = Account(email="verify@example.com", role="client")
            session.add(account)
            session.flush()
            taxpayer = Taxpayer(account_id=account.id, first_name="Verify",
                                last_name="Client", ssn_index="verify-index",
                                resident_state="CA")
            session.add(taxpayer)
            session.flush()
            row = FeeQuoteRecord(taxpayer_id=taxpayer.id, tax_year=2026,
                                 tier="investment", base=D(185), add_ons=D(194),
                                 discount=D(0), total=D(379))
            session.add(row)
            session.flush()

            instruction = request_fee_payment(session, taxpayer, quote=row)
            declare_payment(session, taxpayer,
                            declaration_id=instruction.declaration_id)

            # The point of the whole design: a claim moves nothing.
            before = firm_account(session)
            assert before.fees_collected == D("0.00"), (
                "a client's claim alone counted as revenue"
            )
            assert before.fees_declared_awaiting_confirmation == D("379.00")

            confirm_payment(session, declaration_id=instruction.declaration_id,
                            confirmed_by="verify@practice.example")
            after = firm_account(session)
            assert after.fees_collected == D("379.00"), after.fees_collected
            assert after.net_to_practice == D("379.00")

            # Client money is held apart and never counted as revenue.
            record_funds(session, taxpayer, amount=D(9000), bucket="tax",
                         direction="received", tax_year=2026)
            mixed = firm_account(session)
            assert mixed.fees_collected == D("379.00"), (
                "client tax money leaked into revenue"
            )
            assert mixed.tax_held_in_trust == D("9000.00")
            session.commit()
            return ("claim 0.00 -> confirmed 379.00; 9,000 of client money "
                    "held apart")
        finally:
            session.close()
    finally:
        reset_engine()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reset_engine()


# ===========================================================================
# Output
# ===========================================================================
def _print(report: Report, *, verbose: bool, trace: bool) -> int:
    groups: dict[str, list[Check]] = {}
    for check in report.checks:
        groups.setdefault(check.group, []).append(check)

    print()
    for group, checks in groups.items():
        print(f"  {group}")
        for check in checks:
            if check.skipped:
                mark = "skip"
            elif check.passed:
                mark = " ok "
            else:
                mark = "FAIL"
            print(f"    [{mark}] {check.name.ljust(_NAME)} {check.detail}")
            if not check.passed and not check.skipped and check.remedy:
                import textwrap

                for line in textwrap.wrap(check.remedy, 64):
                    print(f"           {line}")
        print()

    total = len(report.checks)
    print("  " + "─" * 72)
    if report.ok:
        print(f"  ALL {len(report.passed)} CHECKS PASSED"
              + (f", {len(report.skipped)} skipped" if report.skipped else ""))
        print()
        print("  This installation computes returns correctly, reads the forms,")
        print("  seals the data, and books money only when it is confirmed.")
        print()
        print("  It still cannot FILE. That needs an EFIN from IRS e-Services,")
        print("  MeF acceptance testing, and a Form 8879 signed per client.")
        print("  See docs/22-tax-production.md.")
        print()
        return 0

    print(f"  {len(report.failed)} OF {total} CHECKS FAILED")
    print()
    for check in report.failed:
        print(f"    {check.group}: {check.name}")
        print(f"      {check.detail}")
    print()
    print("  Do not take a real client's return through this installation")
    print("  until these pass.")
    print()
    return 1


def _receipt_now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(tzinfo=None)


def _in_throwaway_database(work: Any) -> Any:
    """Run `work(session)` against a fresh database, then put the world back.

    Several checks need a database they can write to without touching the
    one this installation actually uses, and each was doing the same
    save-environment, swap, restore dance. Doing it in one place means a
    check that raises cannot leave the process pointed at a temporary file.
    """
    import os
    import tempfile

    from taxvault.db.session import init_db, new_session, reset_engine

    keys = ("TAXVAULT_HOME", "TAXVAULT_DATABASE_URL", "TAXVAULT_ENV")
    previous = {key: os.environ.get(key) for key in keys}
    workspace = tempfile.mkdtemp(prefix="taxvault-verify-")
    try:
        os.environ.update(
            TAXVAULT_HOME=workspace,
            TAXVAULT_DATABASE_URL=f"sqlite:///{workspace}/verify.db",
        )
        os.environ.pop("TAXVAULT_ENV", None)
        reset_engine()
        init_db()
        session = new_session()
        try:
            outcome = work(session)
            session.commit()
            return outcome
        finally:
            session.close()
    finally:
        reset_engine()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reset_engine()


def _a_client_with_a_quote(session, *, total=None):
    """A taxpayer and an accepted quote, which most money checks need."""
    from taxvault.db.models import Account, FeeQuoteRecord, Taxpayer

    amount = D(215) if total is None else total
    account = Account(email="verify@example.com", role="client")
    session.add(account)
    session.flush()
    taxpayer = Taxpayer(account_id=account.id, first_name="Verify",
                        last_name="Client", ssn_index="verify-index",
                        resident_state="CA")
    session.add(taxpayer)
    session.flush()
    quote = FeeQuoteRecord(taxpayer_id=taxpayer.id, tax_year=2026,
                           tier="standard", base=amount, add_ons=D(0),
                           discount=D(0), total=amount)
    session.add(quote)
    session.flush()
    return taxpayer, quote


def _receipt_check() -> str:
    """A receipt is composed correctly, says what matters, and goes once."""
    def work(session) -> str:
        from taxvault.agent.notify import build_receipt, send_pending_receipts
        from taxvault.engines.revenue import confirm_payment, request_fee_payment

        taxpayer, quote = _a_client_with_a_quote(session)
        instruction = request_fee_payment(session, taxpayer, quote=quote)
        declaration, _ = confirm_payment(
            session, declaration_id=instruction.declaration_id,
            confirmed_by="verify@practice.example",
        )
        receipt = build_receipt(session, declaration)

        assert "215.00" in receipt.body, "the amount is not in the receipt"
        assert declaration.reference in receipt.body, "no reference to quote back"
        # The thing clients actually worry about.
        assert "deducted from your refund" in receipt.body, (
            "the receipt does not say nothing comes out of the refund"
        )
        # A receipt is a phishing template, so it says what it will never ask.
        assert "never ask you for your Social Security number" in receipt.body
        assert receipt.subject.isascii(), (
            "the subject needs MIME encoding, which some filters score"
        )

        # Once only: being told twice reads like being charged twice.
        declaration.receipt_sent_at = _receipt_now()
        session.flush()
        again = send_pending_receipts(session)
        assert again["looked_at"] == 0, "a receipt already sent was queued again"
        return "composed, warns about phishing, sent once"

    return _in_throwaway_database(work)


def _automation_check() -> str:
    """The jobs run against a real database and do not queue duplicates."""
    def work(session) -> str:
        from datetime import timedelta

        from taxvault.agent.automation import pending, run_all

        _, quote = _a_client_with_a_quote(session)
        # Aged, so the unpaid-fee job has something to find.
        quote.created_at = _receipt_now() - timedelta(days=10)
        session.flush()

        first = run_all(session)
        assert first["queued"] >= 1, "the jobs found nothing to do at all"
        second = run_all(session)
        assert second["queued"] == 0, (
            f"a second run queued {second['queued']} duplicate(s)"
        )
        return (f"{len(first['jobs'])} jobs, {first['queued']} queued, "
                f"{len(pending(session, limit=100))} open, no duplicates")

    return _in_throwaway_database(work)
