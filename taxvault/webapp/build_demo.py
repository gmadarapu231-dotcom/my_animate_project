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
