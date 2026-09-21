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
from pathlib import Path

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
    "property_tax": 6000, "mortgage_interest": 13000, "charitable_cash": 2500,
    "existing_401k": 8000, "has_hdhp": True, "hdhp_family": True,
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
            "session": client.get("/api/auth/session", headers=auth).json(),
            "identity": identity,
            "document": document,
            "regular": regular,
            "planning": planning,
            "compare": compare,
            "filings": filings,
            "payment": payment,
        }

    # The token is a real signed credential for a throwaway database, but there
    # is no reason to ship one inside a public file.
    fixtures["identity"].pop("token", None)
    return fixtures


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
        + ";</script>\n<script>\n" + demo_js + "\n</script>\n<script>\n"
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
