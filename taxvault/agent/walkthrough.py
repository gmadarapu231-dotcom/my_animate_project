"""One client, start to finish, against a real database.

`taxvault walkthrough` runs this. It is not a mock: it stands up a real
SQLite database in a temporary directory, drives the real API with the real
middleware, signs a client in, uploads real PDFs, runs the real agent, quotes
the real fee, and walks the money from the client's bank into the practice's
account through the real ledger. Every figure printed came out of the engine.

The point of showing it this way is the money. The interesting moment is step
7, where the client has said they paid and the practice's revenue is still
zero -- because a client's word is not a bank statement, and the only thing
that moves the figure is a preparer confirming a credit against the bank.
"""

from __future__ import annotations

import os
import tempfile
from decimal import Decimal
from typing import Any

from taxvault.money import money

#: Printed widths, so the columns line up in a terminal.
_LABEL = 34


def _rule(title: str = "") -> None:
    if title:
        print()
        print(f"── {title} " + "─" * max(0, 72 - len(title)))
    else:
        print("─" * 76)


def _row(label: str, value: Any, note: str = "") -> None:
    print(f"  {str(label).ljust(_LABEL)} {value}" + (f"   {note}" if note else ""))


def _money(label: str, value: Any, note: str = "") -> None:
    print(f"  {str(label).ljust(_LABEL)} {float(money(value)):>12,.2f}"
          + (f"   {note}" if note else ""))


def run(*, tax_year: int = 2026, scenario: str = "investor",
        practice: str = "Madarapu Tax Associates",
        zelle_address: str = "billing@madarapu-tax.example") -> int:
    """Drive the whole journey and print what happened at each step."""
    workspace = tempfile.mkdtemp(prefix="taxvault-walkthrough-")
    os.environ.update(
        TAXVAULT_HOME=workspace,
        TAXVAULT_DATABASE_URL=f"sqlite:///{workspace}/walkthrough.db",
        TAXVAULT_DEV_CODES="1",
        TAXVAULT_PRACTICE_NAME=practice,
        TAXVAULT_ZELLE_ADDRESS=zelle_address,
    )
    os.environ.pop("TAXVAULT_ENV", None)

    from fastapi.testclient import TestClient

    from taxvault.agent import sample_bundle
    from taxvault.api.app import app
    from taxvault.api.security import reset_rate_limits
    from taxvault.db.session import init_db, new_session, reset_engine

    reset_engine()
    reset_rate_limits()
    init_db()

    print()
    print(f"  {practice}")
    print(f"  A complete engagement, tax year {tax_year}, against a real database.")
    print(f"  Everything below is live output. Nothing is a mock.")

    with TestClient(app) as http:
        # ---------------------------------------------------- 1. the client
        _rule("1. The client signs in")
        email = "jordan@example.com"
        started = http.post("/api/auth/sign-in", json={"email": email}).json()
        _row("code sent to", started["sent_to"])
        token = http.post("/api/auth/verify", json={
            "email": email, "code": started["development_code"],
        }).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        sms = http.post("/api/auth/mobile/start",
                        json={"mobile": "4155550132"}, headers=auth).json()
        http.post("/api/auth/mobile/verify",
                  json={"code": sms["development_code"]}, headers=auth)
        case, documents = sample_bundle(scenario, year=tax_year)
        first, _, last = case.taxpayer_name.partition(" ")
        identity = http.post("/api/auth/identity", headers=auth, json={
            "ssn": "123-45-6789", "email": email, "mobile": "4155550132",
            "first_name": first, "last_name": last.strip(),
            "resident_state": case.resident_state,
        }).json()
        auth = {"Authorization": f"Bearer {identity['token']}"}
        _row("identity verified", f"{case.taxpayer_name}, {identity['ssn']}")
        _row("", "SSN is sealed with AES-GCM; only the last four are ever shown.")

        # ------------------------------------------------- 2. the documents
        _rule("2. The client uploads their documents")
        for document in documents:
            _row(document.filename, f"{len(document.blob):,} bytes")

        # ------------------------------------------------- 3. the agent
        _rule("3. The agent reads them and works out the return")
        response = http.post(
            "/api/agent/run",
            files=[("files", (d.filename, d.blob, d.content_type)) for d in documents],
            data={"tax_year": str(tax_year), "filing_status": case.filing_status,
                  "resident_state": case.resident_state,
                  "age": str(case.situation.get("age", 40))},
            headers=auth,
        )
        if response.status_code != 200:
            print(f"  the agent failed: {response.text}")
            return 1
        agent = response.json()
        for step in agent["steps"]:
            _row(f"[{step['status']}] {step['name']}", step["detail"])

        federal = agent["estimate"]["federal"]
        totals = agent["estimate"]["totals"]
        _rule("4. The return")
        _money("adjusted gross income", federal["agi"])
        _money(f"{federal['deduction_kind']} deduction", federal["deduction_taken"])
        _money("taxable income", federal["taxable_income"])
        _money("federal tax", federal["total_tax"])
        _money("federal withheld", federal["total_payments"])
        for row in agent["estimate"]["states"]:
            _money(f"{row['name']} tax", row["tax"])
        balance = float(money(totals["total_balance"]))
        _money("refund" if balance < 0 else "to pay", abs(balance),
               "federal and state together")

        if agent["open_questions"]:
            _rule("5. What the agent cannot read, so asks")
            for item in agent["open_questions"]:
                print(f"  [{item['severity']}] {item['question']}")
                if item["moves"]:
                    print(f"           worth {item['moves']} on this return")

        # ------------------------------------------------- 6. the fee
        # The documents are stored so the estimate can be persisted, and the
        # quote is priced against THAT -- not against a blank form. A quote
        # taken before the return is computed has no income to band on, and
        # pricing on an unknown income is how work gets given away.
        for document in documents:
            http.post(
                "/api/documents/w2/file" if "w2" in document.filename
                else "/api/documents/form/file",
                files={("file" if "w2" in document.filename else "upload"):
                       (document.filename, document.blob, document.content_type)},
                data={"tax_year": str(tax_year)},
                headers=auth,
            )
        estimate = http.post("/api/estimates", headers=auth, json={
            "tax_year": tax_year, "method": "planning",
            "situation": {"filing_status": case.filing_status,
                          "resident_state": case.resident_state,
                          **case.situation},
        }).json()

        _rule("6. The fee is quoted, before any filing")
        quote = http.post("/api/billing/quote", headers=auth, json={
            "estimate_id": estimate.get("estimate_id"),
            "tax_year": tax_year, "w2_count": 1, "planning_session": True,
        }).json()
        for line in quote["lines"]:
            _money(line["label"], line["amount"])
        _money("TOTAL QUOTED", quote["total"], f"tier: {quote['tier_label']}")
        print()
        print("  The price is set by the work this return takes. It is the same")
        print("  whether the return refunds or owes, and nothing is taken out of")
        print("  the refund -- a refund-based fee is prohibited by Circular 230.")

        requested = http.post("/api/billing/payments/request", headers=auth, json={
            "quote_id": quote["quote_id"], "method": "zelle",
        })
        if requested.status_code != 200:
            # A zero fee is legitimate -- the price list gives a simple
            # low-income return away -- so there is nothing to collect and
            # nothing to confirm.
            _rule("7. Nothing to collect")
            _row("", requested.json().get("detail", requested.text))
            return 0
        request = requested.json()
        _rule("7. The client is asked to pay")
        _money("amount requested", request["amount"])
        _row("pay to", request["pay_to"])
        _row("REFERENCE", request["reference"], "<- goes in the Zelle memo")
        print()
        for step in request["steps"]:
            print(f"    - {step}")

        # ------------------------------------- 8. the client says they paid
        _rule("8. The client says they have sent it")
        declared = http.post("/api/billing/payments/declare", headers=auth, json={
            "declaration_id": request["declaration_id"],
        }).json()
        _row("status", declared["status"])
        _row("", declared["note"])

        # The preparer account, which is who may confirm money.
        session = new_session()
        try:
            from taxvault.db.models import Account

            staff = Account(email="preparer@madarapu-tax.example", role="preparer",
                            email_verified_at=None)
            session.add(staff)
            session.flush()
            staff_id = staff.id
            session.commit()
        finally:
            session.close()

        _rule("9. The practice's account, BEFORE confirming")
        before = _firm_account(platform_model="")
        _money("fees confirmed (revenue)", before["fees_collected"])
        _money("client says paid, unconfirmed", before["fees_declared_awaiting_confirmation"])
        _money("quoted and unpaid", before["fees_outstanding"])
        print()
        print("  Revenue is still zero. A client saying they have paid is a claim,")
        print("  not a bank statement -- and Zelle gives no API by which software")
        print("  could check. The bank is the only witness, so a person has to look.")

        # ------------------------------- 10. reconcile against the bank
        _rule("10. A preparer reconciles the bank export")
        bank_rows = [
            {"description": f"ZELLE FROM {case.taxpayer_name.upper()} "
                            f"MEMO {request['reference']} CONF 88213",
             "amount": float(money(request["amount"])),
             "bank_reference": "CONF88213"},
            {"description": "ZELLE FROM A DIFFERENT CLIENT MEMO NONE GIVEN",
             "amount": 150.00, "bank_reference": "CONF88214"},
        ]
        matched = _reconcile(bank_rows, staff_id)
        for row in matched["matched"]:
            _row("matched", f"{row['reference']}  {float(money(row['amount'])):,.2f}")
        for row in matched["unmatched"]:
            _row("could not match", f"{float(money(row['amount'])):,.2f}  {row['reason']}")
        print()
        print("  Matches are proposed, not booked. A rule that books money on a")
        print("  string match will one day book a client's tax payment as revenue.")

        confirmed = _confirm(request["declaration_id"], staff_id, "CONF88213")
        _rule("11. Confirmed. Now it is revenue.")
        _money("confirmed", confirmed["amount"])
        _row("confirmed by", confirmed["confirmed_by"])
        _row("ledger entry", f"#{confirmed['trust_entry_id']}, bucket "
                             f"'{confirmed['bucket']}'")

        _rule("12. The practice's account, AFTER")
        after = _firm_account(platform_model="")
        _money("fees confirmed (revenue)", after["fees_collected"])
        _money("returns paid", after["returns_paid"]) if False else _row(
            "returns paid", after["returns_paid"])
        _money("average fee", after["average_fee"])
        _money("client money held in trust", after["tax_held_in_trust"],
               "NOT revenue")
        _money("NET TO THE PRACTICE", after["net_to_practice"])

        _rule("13. The same account on a platform plan")
        platform = _firm_account(platform_model="per_return")
        _money("fees confirmed", platform["fees_collected"])
        _money(f"platform fee ({platform['platform_model']})", platform["platform_cost"])
        _money("NET TO THE PRACTICE", platform["net_to_practice"])
        print()
        print("  Only relevant if you white-label to other preparers. Running your")
        print("  own install, the platform cost is zero and you keep all of it.")

        # ----------------------------------------------- 14. the gate
        _rule("14. What still stands between this and the IRS")
        for requirement in agent["gate"]["requirements"]:
            mark = "done" if requirement["met"] else "OPEN"
            note = "" if requirement["satisfiable_in_software"] else "  <- not software"
            _row(f"[{mark}] {requirement['label']}", "", note)
        print()
        print("  The fee is collected and the return is prepared. Filing needs an")
        print("  EFIN from IRS e-Services (45+ days), MeF acceptance testing, and a")
        print("  Form 8879 signed by the client. Until then: prepared, not filed.")
        print()

    reset_engine()
    return 0


def _firm_account(*, platform_model: str) -> dict[str, Any]:
    """Read the firm account straight from the engine, not over HTTP.

    The HTTP route needs a preparer session; this is the same function it
    calls, and a walkthrough should not have to fake a sign-in to read a
    figure it is about to print.
    """
    from datetime import datetime, timedelta, timezone

    from taxvault.db.session import new_session
    from taxvault.engines.revenue import firm_account

    session = new_session()
    try:
        since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=365)
        return firm_account(
            session, since=since, platform_model=platform_model
        ).to_dict()
    finally:
        session.close()


def _reconcile(rows: list[dict[str, Any]], staff_id: int) -> dict[str, Any]:
    from taxvault.db.session import new_session
    from taxvault.engines.revenue import BankRow, reconcile_bank_rows

    session = new_session()
    try:
        parsed = [
            BankRow(description=row["description"],
                    amount=Decimal(str(row["amount"])),
                    bank_reference=row.get("bank_reference", ""))
            for row in rows
        ]
        result = reconcile_bank_rows(
            session, parsed, confirmed_by="preparer@madarapu-tax.example",
            auto_confirm=False,
        )
        session.commit()
        return result.to_dict()
    finally:
        session.close()


def _confirm(declaration_id: int, staff_id: int, bank_reference: str) -> dict[str, Any]:
    from taxvault.db.session import new_session
    from taxvault.engines.revenue import confirm_payment
    from taxvault.money import money as _m

    session = new_session()
    try:
        declaration, entry = confirm_payment(
            session, declaration_id=declaration_id,
            confirmed_by="preparer@madarapu-tax.example",
            bank_reference=bank_reference,
        )
        out = {
            "amount": str(_m(entry.amount)),
            "confirmed_by": declaration.confirmed_by,
            "bucket": declaration.bucket,
            "trust_entry_id": entry.id,
        }
        session.commit()
        return out
    finally:
        session.close()
