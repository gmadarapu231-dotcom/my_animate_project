"""A test environment: synthetic clients, real documents, the whole pipeline.

The point is to be able to answer "does this work?" without a real client's
W-2. Each scenario below builds genuine PDFs -- laid out like the real forms,
with boxes in the right places -- hands them to the agent exactly as an upload
would, and lets the engine do the rest. Nothing is stubbed. If the layout
reader breaks, these scenarios break with it, which is the whole idea.

The SSNs are from the ranges the Social Security Administration has never
issued and never will (area 900-999, which is reserved and invalid by
construction), so a sandbox record can never collide with a real person.

Four scenarios, chosen to cover the four shapes of return a practice actually
sees and the ways each one goes wrong:

  * `simple`        one W-2, standard deduction. The baseline.
  * `investor`      W-2, a consolidated 1099, a mortgage over the ceiling.
  * `early_saver`   a 1099-R with code 1: the 10% and the question behind it.
  * `unreadable`    a scanned document, so the agent has to refuse rather
                    than guess. A test environment that only ever shows the
                    happy path is a demo, not a test.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any, Callable

from taxvault.agent.pipeline import Document

#: SSNs in the 900-999 area are invalid by construction: the SSA has never
#: issued one and the ranges are reserved, so a sandbox number cannot be a
#: real person's. The engine's own validator rejects them, which is correct --
#: the sandbox never puts one through identity verification.
SANDBOX_SSN = "900-00-0000"


class SandboxUnavailable(RuntimeError):
    """reportlab is not installed, so no PDFs can be built."""


def _canvas(width: float = 612, height: float = 792):
    try:
        from reportlab.pdfgen import canvas
    except ImportError as exc:  # pragma: no cover - a build-time dependency
        raise SandboxUnavailable(
            "The sandbox builds real PDFs, which needs reportlab. "
            "Install it with `pip install 'careeros[dev]'`."
        ) from exc
    buffer = io.BytesIO()
    return canvas.Canvas(buffer, pagesize=(width, height)), buffer


# ===========================================================================
# Document builders
# ===========================================================================
def w2_pdf(
    *, employer: str, ein: str, first: str, last: str, year: int,
    wages: float, withheld: float, ss_wages: float | None = None,
    deferral: float = 0.0, state: str = "CA", state_wages: float | None = None,
    state_withheld: float = 0.0,
) -> bytes:
    """A W-2 drawn with the boxes where the real form puts them."""
    c, buffer = _canvas()
    ss_wages = wages if ss_wages is None else ss_wages
    state_wages = wages if state_wages is None else state_wages

    def box(x, y, w, h, label, value, size=7):
        c.setLineWidth(0.6)
        c.rect(x, y, w, h)
        c.setFont("Helvetica", size)
        c.drawString(x + 3, y + h - 9, label)
        if value:
            c.setFont("Helvetica", 9)
            c.drawString(x + 5, y + 5, str(value))

    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, 752, f"{year} Form W-2  Wage and Tax Statement")
    c.setFont("Helvetica", 7)
    c.drawString(400, 752, "Copy B - To Be Filed With Employee's Return")

    box(40, 690, 290, 40, "a  Employee's social security number", SANDBOX_SSN)
    box(40, 646, 290, 40, "b  Employer identification number (EIN)", ein)
    box(40, 580, 290, 62, "c  Employer's name, address, and ZIP code", "")
    c.setFont("Helvetica", 9)
    c.drawString(46, 620, employer)
    c.drawString(46, 608, "1 Industrial Way")
    c.drawString(46, 596, "Fresno, CA 93721")

    box(40, 514, 290, 62, "e  Employee's first name and initial    Last name", "")
    c.setFont("Helvetica", 9)
    c.drawString(46, 554, f"{first}  {last}")
    c.drawString(46, 542, "22 Alder Street")
    c.drawString(46, 530, "Fresno, CA 93722")

    money_boxes = [
        (340, 690, "1  Wages, tips, other compensation", f"{wages:,.2f}"),
        (476, 690, "2  Federal income tax withheld", f"{withheld:,.2f}"),
        (340, 646, "3  Social security wages", f"{ss_wages:,.2f}"),
        (476, 646, "4  Social security tax withheld", f"{ss_wages * 0.062:,.2f}"),
        (340, 602, "5  Medicare wages and tips", f"{ss_wages:,.2f}"),
        (476, 602, "6  Medicare tax withheld", f"{ss_wages * 0.0145:,.2f}"),
        (340, 558, "7  Social security tips", ""),
        (476, 558, "8  Allocated tips", ""),
        (340, 514, "10  Dependent care benefits", ""),
        (476, 514, "11  Nonqualified plans", ""),
    ]
    for x, y, label, value in money_boxes:
        box(x, y, 134, 40, label, value)

    box(340, 448, 134, 62, "12a  See instructions for box 12",
        f"D  {deferral:,.2f}" if deferral else "")
    box(476, 448, 134, 62, "13  Statutory employee", "")
    c.setFont("Helvetica", 7)
    c.drawString(482, 470, "X  Retirement plan")

    box(40, 400, 60, 40, "15  State", state)
    box(100, 400, 110, 40, "Employer's state ID no.", "123-4567-8")
    box(210, 400, 120, 40, "16  State wages, tips, etc.", f"{state_wages:,.2f}")
    box(330, 400, 120, 40, "17  State income tax", f"{state_withheld:,.2f}")
    box(450, 400, 160, 40, "18  Local wages, tips, etc.", "")
    c.save()
    return buffer.getvalue()


def consolidated_1099_pdf(
    *, payer: str, year: int, ordinary_dividends: float, qualified: float,
    gain_distributions: float, short_gain: float, long_gain: float,
    wash_sale: float = 0.0, foreign_tax: float = 0.0,
) -> bytes:
    """A consolidated brokerage statement: a 1099-DIV and a 1099-B in one file."""
    c, buffer = _canvas()
    y = 744

    def line(text, size=9, bold=False, indent=0):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(40 + indent, y, text)
        y -= size + 4

    def row(label, value, indent=0):
        nonlocal y
        c.setFont("Helvetica", 9)
        c.drawString(40 + indent, y, label)
        c.drawRightString(400, y, f"{value:,.2f}")
        y -= 14

    line(f"{year} Consolidated Form 1099", 13, bold=True)
    line(payer, 10)
    y -= 8
    line("Form 1099-DIV   Dividends and Distributions", 10, bold=True)
    y -= 4
    row("1a Total ordinary dividends", ordinary_dividends)
    row("1b Qualified dividends", qualified)
    row("2a Total capital gain distributions", gain_distributions)
    row("2b Unrecap. Sec. 1250 gain", 0.0)
    row("4 Federal income tax withheld", 0.0)
    row("7 Foreign tax paid", foreign_tax)
    y -= 10
    line("Form 1099-B   Proceeds From Broker and Barter Exchange Transactions",
         10, bold=True)
    y -= 4
    row("Total Short-Term proceeds", abs(short_gain) + 38000)
    row("Short-Term cost or other basis", abs(short_gain) + 38000 - short_gain)
    row("Short-Term gain or loss", short_gain)
    row("Total Long-Term proceeds", long_gain + 61000)
    row("Long-Term cost or other basis", 61000.0)
    row("Long-Term gain or loss", long_gain)
    row("1g Wash sale loss disallowed", wash_sale)
    y -= 8
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(40, y, "Basis reported to the IRS for all covered securities.")
    c.save()
    return buffer.getvalue()


def f1099r_pdf(
    *, payer: str, year: int, gross: float, taxable: float, code: str,
    withheld: float, is_ira: bool = False, not_determined: bool = False,
) -> bytes:
    c, buffer = _canvas()
    y = 744

    def row(label, value, right=True):
        nonlocal y
        c.setFont("Helvetica", 9)
        c.drawString(40, y, label)
        if right and value is not None:
            c.drawRightString(380, y, value)
        y -= 15

    c.setFont("Helvetica-Bold", 13)
    c.drawString(40, y, f"{year} Form 1099-R")
    y -= 18
    c.setFont("Helvetica", 9)
    c.drawString(40, y, "Distributions From Pensions, Annuities, Retirement or")
    y -= 12
    c.drawString(40, y, "Profit-Sharing Plans, IRAs, Insurance Contracts, etc.")
    y -= 22
    c.setFont("Helvetica", 8)
    c.drawString(40, y, "PAYER'S name")
    y -= 12
    c.setFont("Helvetica", 10)
    c.drawString(40, y, payer)
    y -= 24

    row("1 Gross distribution", f"{gross:,.2f}")
    row("2a Taxable amount", f"{taxable:,.2f}")
    if not_determined:
        row("2b Taxable amount not determined", None, right=False)
    row("3 Capital gain (included in box 2a)", "0.00")
    row("4 Federal income tax withheld", f"{withheld:,.2f}")
    row("5 Employee contributions/Designated Roth contributions", "0.00")
    row("6 Net unrealized appreciation in employer's securities", "0.00")
    row(f"7 Distribution code(s)  {code}", None, right=False)
    if is_ira:
        row("IRA/SEP/SIMPLE", None, right=False)
    row("14 State tax withheld", "0.00")
    c.save()
    return buffer.getvalue()


def f1098_pdf(
    *, lender: str, year: int, interest: float, principal: float,
    origination: str, insurance: float = 0.0, points: float = 0.0,
) -> bytes:
    c, buffer = _canvas()
    y = 744

    def row(label, value):
        nonlocal y
        c.setFont("Helvetica", 9)
        c.drawString(40, y, label)
        if value is not None:
            c.drawRightString(400, y, value)
        y -= 16

    c.setFont("Helvetica-Bold", 13)
    c.drawString(40, y, f"Form 1098  Mortgage Interest Statement  {year}")
    y -= 24
    c.setFont("Helvetica", 8)
    c.drawString(40, y, "RECIPIENT'S/LENDER'S name")
    y -= 12
    c.setFont("Helvetica", 10)
    c.drawString(40, y, lender)
    y -= 24

    row("1 Mortgage interest received from payer(s)/borrower(s)", f"{interest:,.2f}")
    row("2 Outstanding mortgage principal", f"{principal:,.2f}")
    row(f"3 Mortgage origination date   {origination}", None)
    row("4 Refund of overpaid interest", "0.00")
    row("5 Mortgage insurance premiums", f"{insurance:,.2f}")
    row("6 Points paid on purchase of principal residence", f"{points:,.2f}")
    row("9 Number of properties securing the mortgage", "1")
    c.save()
    return buffer.getvalue()


def scanned_pdf(label: str = "1099-INT") -> bytes:
    """A PDF with no text layer: a photograph of a form, as clients send.

    Drawn as lines and rectangles only. There is nothing to extract, which is
    the point: the agent has to say so rather than invent figures.
    """
    c, buffer = _canvas()
    c.setLineWidth(1)
    c.rect(60, 500, 480, 220)
    for i in range(7):
        c.setLineWidth(0.4)
        c.line(80, 690 - i * 26, 520, 690 - i * 26)
    for x in (160, 300, 440):
        c.rect(x, 520, 70, 14, fill=0)
    c.save()
    return buffer.getvalue()


# ===========================================================================
# Scenarios
# ===========================================================================
@dataclass
class Scenario:
    key: str
    label: str
    description: str
    expect: str
    taxpayer_name: str
    filing_status: str = "single"
    resident_state: str = "CA"
    situation: dict[str, Any] = field(default_factory=dict)
    build: Callable[[int], list[Document]] = None  # type: ignore[assignment]

    def documents(self, year: int) -> list[Document]:
        return self.build(year)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "description": self.description,
            "expect": self.expect, "taxpayer_name": self.taxpayer_name,
            "filing_status": self.filing_status, "resident_state": self.resident_state,
        }


def _simple(year: int) -> list[Document]:
    return [Document(
        filename="w2-fresno-produce.pdf", content_type="application/pdf",
        blob=w2_pdf(employer="Fresno Produce Co", ein="94-1234567",
                    first="Alex", last="Rivera", year=year,
                    wages=68000, withheld=7400, deferral=4000,
                    state="CA", state_withheld=2650),
    )]


def _investor(year: int) -> list[Document]:
    return [
        Document(filename="w2-northstar.pdf", content_type="application/pdf",
                 blob=w2_pdf(employer="Northstar Systems Inc", ein="94-7654321",
                             first="Priya", last="Raman", year=year,
                             wages=196000, withheld=31200, deferral=23500,
                             state="CA", state_withheld=14100)),
        Document(filename="consolidated-1099.pdf", content_type="application/pdf",
                 blob=consolidated_1099_pdf(payer="Meridian Brokerage LLC", year=year,
                                            ordinary_dividends=6480, qualified=5905,
                                            gain_distributions=1240,
                                            short_gain=-6600, long_gain=26150,
                                            wash_sale=910, foreign_tax=86)),
        Document(filename="form-1098.pdf", content_type="application/pdf",
                 blob=f1098_pdf(lender="Cascade Mutual Bank", year=year,
                                interest=31400, principal=812000,
                                origination="06/14/2019", insurance=1860)),
    ]


def _early_saver(year: int) -> list[Document]:
    return [
        Document(filename="w2-valley-clinic.pdf", content_type="application/pdf",
                 blob=w2_pdf(employer="Valley Clinic Partners", ein="94-2468024",
                             first="Dana", last="Okonkwo", year=year,
                             wages=84000, withheld=9800,
                             state="CA", state_withheld=3900)),
        Document(filename="1099r-meridian.pdf", content_type="application/pdf",
                 blob=f1099r_pdf(payer="Meridian 401(k) Plan", year=year,
                                 gross=42000, taxable=42000, code="1",
                                 withheld=8400, not_determined=True)),
    ]


def _unreadable(year: int) -> list[Document]:
    return [
        Document(filename="w2-harbour-foods.pdf", content_type="application/pdf",
                 blob=w2_pdf(employer="Harbour Foods LLC", ein="94-1112223",
                             first="Sam", last="Delacroix", year=year,
                             wages=52000, withheld=4900,
                             state="TX", state_withheld=0)),
        Document(filename="photo-of-1099int.pdf", content_type="application/pdf",
                 blob=scanned_pdf("1099-INT")),
    ]


SCENARIOS: dict[str, Scenario] = {
    "simple": Scenario(
        key="simple", label="One W-2, nothing else",
        description=(
            "A single job, a 401(k) deferral, California. The baseline: if this "
            "one is wrong, nothing else matters."
        ),
        expect="A clean read and a refund, with a couple of things to confirm.",
        taxpayer_name="Alex Rivera", filing_status="single", resident_state="CA",
        situation={"age": 31}, build=_simple,
    ),
    "investor": Scenario(
        key="investor", label="W-2, brokerage and a mortgage",
        description=(
            "A consolidated 1099 with a short-term loss against a long-term gain "
            "and a wash sale, plus a mortgage over the $750,000 ceiling. Exercises "
            "Schedule D netting and the debt-limit proration together."
        ),
        expect=(
            "The short-term loss offsets long-term gain and the survivor stays "
            "long-term; mortgage interest is prorated, not deducted in full."
        ),
        taxpayer_name="Priya Raman", filing_status="married_jointly",
        resident_state="CA",
        situation={"age": 44, "spouse_age": 43, "children_under_17": 2,
                   "state_local_income_tax": 14100, "property_tax": 9800},
        build=_investor,
    ),
    "early_saver": Scenario(
        key="early_saver", label="An early 401(k) withdrawal",
        description=(
            "A 1099-R with box 7 code 1 and box 2b ticked. The 10% additional tax "
            "applies, and two questions decide whether it should."
        ),
        expect=(
            "The 10% is charged, and the agent asks about the rule-of-55 exception "
            "and about IRA basis rather than assuming either."
        ),
        taxpayer_name="Dana Okonkwo", filing_status="single", resident_state="CA",
        situation={"age": 56}, build=_early_saver,
    ),
    "unreadable": Scenario(
        key="unreadable", label="A photographed form",
        description=(
            "One good W-2 and one scan with no text layer. A test environment that "
            "only shows the happy path is a demo, not a test."
        ),
        expect=(
            "The agent refuses to guess: the scan becomes a blocking question and "
            "the run does not reach ready-for-signature."
        ),
        taxpayer_name="Sam Delacroix", filing_status="single", resident_state="TX",
        situation={"age": 38}, build=_unreadable,
    ),
}


def scenarios() -> list[dict[str, Any]]:
    return [s.to_dict() for s in SCENARIOS.values()]


def sample_bundle(key: str = "investor", *, year: int | None = None
                  ) -> tuple[Scenario, list[Document]]:
    from taxvault.config import latest_year

    if key not in SCENARIOS:
        raise KeyError(f"{key!r} is not a sandbox scenario; have {sorted(SCENARIOS)}")
    scenario = SCENARIOS[key]
    target = year or latest_year()
    return scenario, scenario.documents(target)
