"""Build a single-file demo of the client.

The demo has to be honest to be worth anything, so the fixtures are not written
by hand: this drives the real API in-process with `TestClient`, captures what
the engine actually returns for a sample 2025 return, and inlines those
responses. Everything else -- stylesheet, client, logo -- is inlined too, so
the result is one file that opens from a disk with no server, no network and no
build tooling.

    python -m taxvault.webapp.build_demo [output.html]
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent

#: The sample return the demo shows. A dual-income California household with
#: two children: common enough to be recognisable, and it exercises the child
#: credit, the childcare credit, a state return and the planning engine.
SAMPLE_W2 = {
    "tax_year": 2025,
    "employer_name": "Acme Corporation",
    "employer_ein": "12-3456789",
    "box1_wages": 118000,
    "box2_federal_withheld": 14200,
    "box3_social_security_wages": 126000,
    "box4_social_security_withheld": 7812,
    "box5_medicare_wages": 126000,
    "box6_medicare_withheld": 1827,
    "box12": {"D": 8000},
    "box13": {"retirement_plan": True},
    "states": [{"state": "CA", "state_wages": 118000, "state_withheld": 6800}],
}
SAMPLE_SITUATION = {
    "filing_status": "married_jointly", "resident_state": "CA",
    "age": 43, "spouse_age": 41, "children_under_17": 2,
    "dependent_care_expenses": 7000, "state_local_income_tax": 6800,
    "property_tax": 6000, "charitable_cash": 2500,
    "existing_401k": 8000, "has_hdhp": True, "hdhp_family": True,

    # Shares: a short-term loss against a long-term gain, plus a loss carried
    # in from an earlier year. Chosen so the demo shows the thing clients get
    # wrong -- the survivor of the netting keeps LONG-term treatment.
    "sales": {
        "short_term": -4200,
        "long_term": 16800,
        "capital_gain_distributions": 1240,
        "carryforward_long": 3000,
        "wash_sale_disallowed": 640,
    },

    # A rollover: reported, and not taxable. It shows the 1099-R table without
    # pretending this household took an early withdrawal.
    "distributions": [{
        "payer": "Meridian 401(k) Plan",
        "gross": 64000, "code": "G", "plan_kind": "401k", "age_at_distribution": 43,
    }],

    # The mortgage, as Form 1098 states it rather than as one number: a
    # balance over the ceiling, so the demo shows the proration.
    "loans": [{
        "lender": "Cascade Mutual Bank",
        "balance": 812000, "interest_paid": 31400,
        "mortgage_insurance": 1860, "origination": "2019-06-14",
        "used_for": "purchase", "kind": "acquisition", "is_main_home": True,
    }],

    "plan": {
        "traditional_401k": 8000, "roth_401k": 0,
        "employer_contribution": 5900, "compensation": 118000,
    },
}


def _sample_w2_pdf() -> bytes | None:
    """A W-2 laid out as the real form is, for the demo capture.

    Returns None when reportlab is not installed -- it is a build-time
    dependency, and the build falls back to typed boxes rather than failing.
    """
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
    except ImportError:
        return None

    import io

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)

    def box(x, y, w, h, label, value, small=7):
        c.setLineWidth(0.6)
        c.rect(x, y, w, h)
        c.setFont("Helvetica", small)
        c.drawString(x + 3, y + h - 9, label)
        if value:
            c.setFont("Helvetica", 9)
            c.drawString(x + 5, y + 5, value)

    box(40, 640, 300, 46, "a  Employee's social security number", "123-45-6789")
    box(40, 586, 300, 48, "b  Employer identification number (EIN)", "12-3456789")
    box(40, 496, 300, 84, "c  Employer's name, address, and ZIP code", "")
    c.setFont("Helvetica", 9)
    c.drawString(45, 556, "NORTHWIND LOGISTICS LLC")
    c.drawString(45, 544, "1400 Harbor Parkway, Suite 210")
    box(40, 400, 300, 90, "e  Employee's first name and initial   Last name", "")
    c.setFont("Helvetica", 9)
    c.drawString(45, 460, "DANA J")
    c.drawString(130, 460, "REED")
    c.drawString(45, 440, "88 Cedar Street Apt 4B")

    rows = [
        ("1  Wages, tips, other compensation", "118,000.00",
         "2  Federal income tax withheld", "14,200.00"),
        ("3  Social security wages", "126,000.00",
         "4  Social security tax withheld", "7,812.00"),
        ("5  Medicare wages and tips", "126,000.00",
         "6  Medicare tax withheld", "1,827.00"),
        ("7  Social security tips", "", "8  Allocated tips", ""),
        ("9", "", "10  Dependent care benefits", ""),
        ("11  Nonqualified plans", "", "12a  See instructions for box 12", "D  8,000.00"),
    ]
    y = 640
    for l1, v1, l2, v2 in rows:
        box(350, y, 115, 46, l1, v1)
        box(465, y, 115, 46, l2, v2)
        y -= 46
    box(350, y, 115, 46, "13  Statutory  Retirement  Third-party", "X  Retirement plan", small=6)
    y -= 98

    heads = ["15 State", "Employer's state ID number", "16 State wages, tips, etc.",
             "17 State income tax"]
    vals = ["CA", "123-4567-8", "118,000.00", "6,800.00"]
    for x, w, head, value in zip([40, 95, 210, 310], [55, 115, 100, 85], heads, vals):
        box(x, y, w, 40, head, value, small=6)

    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y - 24, "Form W-2   Wage and Tax Statement                    2025")
    c.save()
    return buffer.getvalue()


def capture() -> dict:
    """Run the real journey against the real app and keep every response."""
    workspace = tempfile.mkdtemp(prefix="taxvault-demo-")
    os.environ.update(
        TAXVAULT_HOME=workspace,
        TAXVAULT_DATABASE_URL=f"sqlite:///{workspace}/demo.db",
        TAXVAULT_DEV_CODES="1",
    )
    from fastapi.testclient import TestClient

    from taxvault.api.app import app
    from taxvault.api.security import reset_rate_limits
    from taxvault.db.session import init_db, reset_engine

    reset_engine()
    reset_rate_limits()
    init_db()

    with TestClient(app) as client:
        email = "dana@example.com"
        started = client.post("/api/auth/sign-in", json={"email": email})
        token = client.post("/api/auth/verify", json={
            "email": email, "code": started.json()["development_code"],
        }).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        sms = client.post("/api/auth/mobile/start",
                          json={"mobile": "4155550132"}, headers=auth)
        client.post("/api/auth/mobile/verify",
                    json={"code": sms.json()["development_code"]}, headers=auth)
        identity = client.post("/api/auth/identity", headers=auth, json={
            "ssn": "123-45-6789", "email": email, "mobile": "4155550132",
            "first_name": "Dana", "last_name": "Reed", "resident_state": "CA",
        }).json()
        auth = {"Authorization": f"Bearer {identity['token']}"}

        # Upload a form-shaped PDF rather than typing boxes, so the demo shows
        # what the layout reader actually pulls off a W-2 -- names included.
        pdf = _sample_w2_pdf()
        if pdf is not None:
            document = client.post(
                "/api/documents/w2/file",
                files={"file": ("w2.pdf", pdf, "application/pdf")},
                data={"tax_year": "2025"},
                headers=auth,
            ).json()
        else:
            document = client.post("/api/documents/w2/boxes",
                                   json=SAMPLE_W2, headers=auth).json()

        def estimate(method: str) -> dict:
            return client.post("/api/estimates", headers=auth, json={
                "tax_year": 2025, "method": method, "situation": SAMPLE_SITUATION,
            }).json()

        regular = estimate("regular")
        planning = estimate("planning")
        compare = client.post("/api/estimates/compare", headers=auth, json={
            "tax_year": 2025, "situation": SAMPLE_SITUATION,
        }).json()
        filings = client.get("/api/filings/history", headers=auth).json()
        payment = client.post("/api/payments/choose", headers=auth, json={
            "estimate_id": regular["estimate_id"], "method": "direct_deposit",
            "bank": {"routing_number": "121000248", "account_number": "000123456789"},
        }).json()

        fixtures = {
            "years": client.get("/api/reference/years").json(),
            "states": client.get("/api/reference/states").json(),
            "irs": client.get("/api/reference/irs?state_code=CA").json(),
            "session": client.get("/api/auth/session", headers=auth).json(),
            "identity": identity,
            "document": document,
            "regular": regular,
            "planning": planning,
            "compare": compare,
            "filings": filings,
            "payment": payment,
            # The handoff is shown against a balance, so capture one even
            # though the sample return refunds.
            "handoff": client.get("/api/payments/handoff", headers=auth, params={
                "amount": 3261.14, "tax_year": 2025,
                "jurisdiction": "federal", "method": "irs_direct_pay",
            }).json(),
            # The agent, run for real against synthetic documents, plus the
            # money flow from quote to confirmed revenue. Captured here rather
            # than written by hand so the demo cannot drift from the engine.
            "agent": _capture_agent(client),
            "money": _capture_money(client, auth),
            # The jobs that run without anyone asking, and the receipt a
            # client gets once their money is booked. Captured after the
            # money flow, because what the jobs find depends on it.
            "automation": _capture_automation(client, auth),
            # The downloadable estimate, drawn by the real renderer. Carried
            # whole so the demo's download button hands over the actual PDF
            # rather than a picture of one.
            "estimate_document": _capture_estimate_document(client, auth, regular),
            "pricing": client.get("/api/agent/pricing").json(),
            "retirement_reference": client.get("/api/reference/retirement").json(),
            "home_loans_reference": client.get("/api/reference/home-loans").json(),
            # One worked RMD, so the demo's button has something real to show.
            "rmd": client.get("/api/reference/rmd", params={
                "birth_year": 1953, "balance": 500000, "taken": 0,
            }).json(),
            "service_fee": client.get("/api/payments/service-fee", headers=auth).json(),
            "fee": client.post("/api/billing/quote", headers=auth, json={
                "estimate_id": regular["estimate_id"], "tax_year": 2025, "w2_count": 1,
            }).json(),
        }

    # The token is a real signed credential for a throwaway database, but there
    # is no reason to ship one inside a public file.
    fixtures["identity"].pop("token", None)
    return fixtures


def _capture_agent(client) -> dict:
    """Run the agent on every sandbox scenario and keep the whole trace.

    `answer_everything=true` runs each one twice: once to collect the
    questions, then again with them answered, which is how a return reaches
    ready_for_signature. The demo shows both, because the gap between them is
    the product.
    """
    from taxvault.agent.sandbox import SCENARIOS

    runs = {}
    for key in SCENARIOS:
        response = client.post(
            f"/api/agent/sandbox/{key}?year=2026&answer_everything=true"
        )
        if response.status_code == 200:
            runs[key] = response.json()
    return {
        "scenarios": client.get("/api/agent/scenarios").json()["scenarios"],
        "runs": runs,
    }


def _capture_money(client, auth) -> dict:
    """Quote, request, declare, confirm -- and the firm account either side.

    The interesting frame is the third one: the client has said they paid and
    the practice's revenue is still zero, because Zelle gives software no way
    to witness a transfer and the bank is the only witness.
    """
    from datetime import datetime, timedelta, timezone

    from taxvault.db.models import Account
    from taxvault.db.session import new_session
    from taxvault.engines.revenue import confirm_payment, firm_account

    estimate = client.post("/api/estimates", headers=auth, json={
        "tax_year": 2025, "method": "planning", "situation": SAMPLE_SITUATION,
    }).json()
    quote = client.post("/api/billing/quote", headers=auth, json={
        "estimate_id": estimate.get("estimate_id"), "tax_year": 2025,
        "w2_count": 1, "planning_session": True,
    }).json()
    requested = client.post("/api/billing/payments/request", headers=auth, json={
        "quote_id": quote["quote_id"], "method": "zelle",
        "pay_to": "billing@your-practice.example",
    }).json()
    declared = client.post("/api/billing/payments/declare", headers=auth, json={
        "declaration_id": requested["declaration_id"],
    }).json()

    session = new_session()
    try:
        since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=365)
        before = firm_account(session, since=since).to_dict()
        staff = Account(email="preparer@your-practice.example", role="preparer")
        session.add(staff)
        session.flush()
        declaration, entry = confirm_payment(
            session, declaration_id=requested["declaration_id"],
            confirmed_by="preparer@your-practice.example",
            bank_reference="ZELLE CONF 88213",
        )
        confirmed = {
            "amount": str(entry.amount), "confirmed_by": declaration.confirmed_by,
            "bucket": declaration.bucket, "trust_entry_id": entry.id,
            "bank_reference": declaration.bank_reference,
        }
        after = firm_account(session, since=since).to_dict()
        platform = firm_account(session, since=since,
                                platform_model="per_return").to_dict()
        session.commit()
    finally:
        session.close()

    return {
        "quote": quote,
        "requested": requested,
        "declared": declared,
        "confirmed": confirmed,
        "before": before,
        "after": after,
        "platform": platform,
        "bank_rows": [
            {"description": f"ZELLE FROM DANA REED ON 10/08 MEMO "
                            f"{requested['reference']} CONF 88213",
             "amount": requested["amount"], "matched": True},
            {"description": "ZELLE FROM ANOTHER CLIENT MEMO NONE GIVEN",
             "amount": "150.00", "matched": False},
        ],
    }

def _capture_automation(client, auth) -> dict:
    """The unprompted jobs, the work queue they fill, and a real receipt.

    Three things are shown and all three are captured, not written:

      * a second fee, requested and declared but NOT confirmed, so
        `flag_awaiting_confirmation` has something true to find;
      * a bank export run through `reconcile`, which queues the match and
        books nothing, because the bank is the only witness to a transfer;
      * the receipt itself, composed from the confirmed payment.

    The receipt is composed in dry-run. A demo file has no mail server and
    the honest thing is to show the client exactly what would be sent rather
    than invent a delivery that did not happen.
    """
    from datetime import date

    from taxvault.agent import automation
    from taxvault.agent.notify import send_pending_receipts
    from taxvault.db.session import new_session
    from taxvault.engines.revenue import BankRow

    # A fee the client says they have paid and nobody has checked yet. This
    # is the state a practice actually loses money in, so the demo shows the
    # job that catches it.
    # 2026, because the 2025 fee was confirmed a moment ago and the engine
    # correctly refuses to ask for a fee that is already paid.
    second = client.post("/api/billing/quote", headers=auth, json={
        "tax_year": 2026, "w2_count": 2, "schedule_d": True,
    }).json()
    waiting = client.post("/api/billing/payments/request", headers=auth, json={
        "quote_id": second["quote_id"], "method": "zelle",
        "pay_to": "billing@your-practice.example",
    }).json()
    client.post("/api/billing/payments/declare", headers=auth, json={
        "declaration_id": waiting["declaration_id"],
    })

    session = new_session()
    try:
        today = date.today()
        jobs = [
            automation.watch_deadlines(session, as_of=today),
            automation.review_new_documents(session),
            automation.chase_questions(session),
            automation.chase_unpaid_fees(session, as_of=today),
            automation.flag_awaiting_confirmation(session),
        ]
        # A bank export with one credit that matches, one that is short, and
        # one with no reference at all -- the three outcomes a real export has.
        bank = automation.reconcile(session, [
            BankRow(description=f"ZELLE FROM DANA REED MEMO {waiting['reference']}",
                    amount=Decimal(str(waiting["amount"])),
                    bank_reference="ZELLE CONF 90114"),
            BankRow(description=f"ZELLE FROM D REED MEMO {waiting['reference']}",
                    amount=Decimal("25.00"), bank_reference="ZELLE CONF 90115"),
            BankRow(description="ZELLE FROM SOMEBODY ELSE NO MEMO",
                    amount=Decimal("150.00"), bank_reference="ZELLE CONF 90116"),
        ], proposed_by="preparer@your-practice.example")
        receipts = send_pending_receipts(
            session, practice="Your Tax Practice", dry_run=True
        )
        queue = automation.pending(session, limit=50)
        session.commit()
    finally:
        session.close()

    return {
        "jobs": [job.to_dict() for job in jobs],
        "queued": sum(job.queued for job in jobs),
        "bank": bank,
        "queue": queue,
        "receipts": receipts,
        "never_automated": [
            {"what": "Confirming money against a bank statement",
             "why": automation.HUMAN_BANK},
            {"what": "A client's review and their Form 8879",
             "why": automation.HUMAN_SIGNATURE},
            {"what": "Transmitting a return to the IRS",
             "why": automation.HUMAN_EFILE},
        ],
    }


def _capture_estimate_document(client, auth, estimate) -> dict:
    """The estimate document, both ways, exactly as the endpoint serves it.

    A demo that shows a download button and then cannot produce the file is
    worse than one that hides it, so the real bytes travel inside the page.
    The PDF is a few kilobytes; base64 makes it a third larger again, which is
    a fair price for a button that actually works offline.
    """
    estimate_id = estimate.get("estimate_id")
    if not estimate_id:
        return {}
    out: dict[str, Any] = {"estimate_id": estimate_id}
    pdf = client.get(f"/api/estimates/{estimate_id}/document?format=pdf", headers=auth)
    if pdf.status_code == 200 and pdf.headers.get("content-type") == "application/pdf":
        out["pdf_base64"] = base64.b64encode(pdf.content).decode("ascii")
        out["filename"] = _filename_from(pdf.headers.get("content-disposition", ""))
    page = client.get(
        f"/api/estimates/{estimate_id}/document?format=html", headers=auth)
    if page.status_code == 200:
        out["html"] = page.text
    return out


def _filename_from(disposition: str) -> str:
    match = re.search(r'filename="([^"]+)"', disposition or "")
    return match.group(1) if match else "estimate.pdf"


def inline_svg(name: str) -> str:
    """A data: URI, so the single file has no sibling assets to lose."""
    raw = (HERE / name).read_bytes()
    return "data:image/svg+xml;base64," + base64.b64encode(raw).decode("ascii")


def build(destination: Path) -> Path:
    fixtures = capture()
    html = (HERE / "index.html").read_text(encoding="utf-8")
    css = (HERE / "styles.css").read_text(encoding="utf-8")
    app_js = (HERE / "app.js").read_text(encoding="utf-8")
    demo_js = (HERE / "demo.js").read_text(encoding="utf-8")
    # The demo reads uploads itself, so it carries a PDF reader and the W-2
    # finder. Both must load before `demo.js` installs the transport.
    pdf_js = (HERE / "pdfread.js").read_text(encoding="utf-8")
    w2_js = (HERE / "w2read.js").read_text(encoding="utf-8")

    mark, logo = inline_svg("mark.svg"), inline_svg("logo.svg")

    html = html.replace('<link rel="stylesheet" href="/static/styles.css">',
                        f"<style>\n{css}\n{BANNER_CSS}\n</style>")
    html = html.replace('<link rel="manifest" href="/manifest.webmanifest">', "")
    # Opened from a disk there is no site root, so the brand link would navigate
    # the browser to the filesystem. It stays as a home affordance and does
    # nothing, which is correct for a single page with no routes.
    html = html.replace('<a class="brand" href="/"', '<a class="brand" href="#"')
    html = html.replace('href="/static/mark.svg"', f'href="{mark}"')
    html = html.replace('src="/static/mark.svg"', f'src="{mark}"')
    html = html.replace(
        '<script src="/static/app.js"></script>',
        "<script>window.TAXVAULT_FIXTURES = "
        + json.dumps(fixtures, separators=(",", ":"))
        + ";</script>\n<script>\n" + pdf_js + "\n</script>\n<script>\n"
        + w2_js + "\n</script>\n<script>\n" + demo_js + "\n</script>\n<script>\n"
        + app_js.replace("'/static/logo.svg'", f"'{logo}'")
                .replace('"/static/logo.svg"', f'"{logo}"')
        + "\n</script>",
    )
    # The service worker needs an origin; a file:// page has none.
    html = html.replace("<title>TaxVault</title>", "<title>TaxVault — demo</title>")

    destination.write_text(html, encoding="utf-8")
    return destination


BANNER_CSS = """
/* Demo banner. Fixed to the top on a wide screen and to the bottom on a phone,
   where the tab bar already owns the foot of the window. */
.demo-banner {
  position: fixed; left: 0; right: 0; top: 0; z-index: 60;
  background: var(--gold); color: #1a1405;
  font-size: 12.5px; line-height: 1.45; padding: 8px 14px; text-align: center;
}
.demo-banner code { background: rgba(0,0,0,.14); padding: 1px 5px; border-radius: 4px; }
body { padding-top: 54px; }
@media (max-width: 819px) { body { padding-top: 66px; } }
"""


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    destination = Path(argv[0]) if argv else Path("taxvault-demo.html")
    built = build(destination)
    size = built.stat().st_size
    print(f"wrote {built} ({size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
