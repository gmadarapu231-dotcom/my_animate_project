"""The whole client journey, through the real HTTP API.

This is the test that matters: sign in, prove identity, upload a W-2, get an
estimate, switch to planning, check prior years, choose how to pay. If this
passes, the product works; if it fails, nothing else being green helps.
"""

from __future__ import annotations

import pytest


def sign_in(client, email="dana@example.com"):
    started = client.post("/api/auth/sign-in", json={"email": email})
    assert started.status_code == 200, started.text
    code = started.json()["development_code"]
    verified = client.post("/api/auth/verify", json={"email": email, "code": code})
    assert verified.status_code == 200, verified.text
    return verified.json()["token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def verify_identity(client, token, *, ssn="123-45-6789", email="dana@example.com",
                    mobile="4155550132", state="CA"):
    started = client.post("/api/auth/mobile/start", json={"mobile": mobile}, headers=auth(token))
    assert started.status_code == 200, started.text
    client.post("/api/auth/mobile/verify",
                json={"code": started.json()["development_code"]}, headers=auth(token))
    done = client.post("/api/auth/identity", headers=auth(token), json={
        "ssn": ssn, "email": email, "mobile": mobile,
        "first_name": "Dana", "last_name": "Reed", "resident_state": state,
    })
    assert done.status_code == 200, done.text
    return done.json()["token"]


W2_2025 = {
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


# ---------------------------------------------------------------------------
# the gates
# ---------------------------------------------------------------------------
def test_health_is_honest_about_not_being_an_efile_provider(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["e_file"] is False
    assert len(body["no_income_tax_states"]) == 9


def test_tax_data_requires_a_session(client):
    assert client.get("/api/documents").status_code == 401
    assert client.post("/api/estimates", json={}).status_code == 401


def test_signing_in_is_not_enough_to_read_tax_data(client):
    """The whole point of the second gate: an email is not an identity."""
    token = sign_in(client)
    response = client.get("/api/documents", headers=auth(token))
    assert response.status_code == 403
    assert "Social Security number" in response.json()["detail"]


def test_wrong_code_is_refused_and_counts_down(client):
    client.post("/api/auth/sign-in", json={"email": "dana@example.com"})
    response = client.post("/api/auth/verify",
                           json={"email": "dana@example.com", "code": "000000"})
    assert response.status_code == 401
    assert "attempt(s) left" in response.json()["detail"]


def test_an_invalid_ssn_is_refused(client):
    token = sign_in(client)
    started = client.post("/api/auth/mobile/start", json={"mobile": "4155550132"},
                          headers=auth(token))
    client.post("/api/auth/mobile/verify",
                json={"code": started.json()["development_code"]}, headers=auth(token))
    response = client.post("/api/auth/identity", headers=auth(token), json={
        "ssn": "666-12-3456", "email": "dana@example.com", "mobile": "4155550132",
    })
    assert response.status_code == 400
    assert "not a valid Social Security number" in response.json()["detail"]


def test_identity_requires_a_verified_mobile(client):
    token = sign_in(client)
    response = client.post("/api/auth/identity", headers=auth(token), json={
        "ssn": "123-45-6789", "email": "dana@example.com", "mobile": "4155550132",
    })
    assert response.status_code == 400
    assert "Verify your mobile number first" in response.json()["detail"]


def test_identity_refuses_a_mobile_that_is_not_the_verified_one(client):
    """Verifying one number then claiming another must not pass."""
    token = sign_in(client)
    started = client.post("/api/auth/mobile/start", json={"mobile": "4155550132"},
                          headers=auth(token))
    client.post("/api/auth/mobile/verify",
                json={"code": started.json()["development_code"]}, headers=auth(token))
    response = client.post("/api/auth/identity", headers=auth(token), json={
        "ssn": "123-45-6789", "email": "dana@example.com", "mobile": "4155559999",
    })
    assert response.status_code == 400
    assert "not the mobile number verified" in response.json()["detail"]


def test_identity_accepts_an_omitted_mobile_once_one_is_verified(client):
    """The server already holds the proven number; re-typing it proves nothing."""
    token = sign_in(client)
    started = client.post("/api/auth/mobile/start", json={"mobile": "4155550132"},
                          headers=auth(token))
    client.post("/api/auth/mobile/verify",
                json={"code": started.json()["development_code"]}, headers=auth(token))
    response = client.post("/api/auth/identity", headers=auth(token), json={
        "ssn": "123-45-6789", "email": "dana@example.com", "mobile": "",
    })
    assert response.status_code == 200, response.text
    assert response.json()["identity_verified"] is True


def test_ssn_is_never_returned_in_full(client):
    token = verify_identity(client, sign_in(client))
    body = client.get("/api/auth/session", headers=auth(token)).json()
    assert body["taxpayer"]["ssn"] == "***-**-6789"
    assert "123456789" not in client.get("/api/auth/session", headers=auth(token)).text


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------
def test_w2_upload_and_duplicate_detection(client):
    token = verify_identity(client, sign_in(client))
    first = client.post("/api/documents/w2/boxes", json=W2_2025, headers=auth(token))
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "parsed"
    assert first.json()["warnings"] == []

    again = client.post("/api/documents/w2/boxes", json=W2_2025, headers=auth(token))
    assert again.status_code == 409
    assert "twice would double the wages" in again.json()["detail"]


def test_a_mistyped_box_is_caught_on_upload(client):
    token = verify_identity(client, sign_in(client))
    broken = {**W2_2025, "box4_social_security_withheld": 5000}
    response = client.post("/api/documents/w2/boxes", json=broken, headers=auth(token))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "needs_review"
    assert any(w["severity"] == "error" and w["box"] == "4" for w in body["warnings"])


def test_an_unreadable_upload_is_stored_but_not_guessed_at(client):
    """A photo is pixels. It is kept, and the response says it was not read."""
    token = verify_identity(client, sign_in(client))
    response = client.post(
        "/api/documents/w2/file",
        files={"file": ("w2.jpg", b"\xff\xd8\xff\xe0 jpeg bytes", "image/jpeg")},
        data={"tax_year": "2025"},
        headers=auth(token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["parse_confidence"] == 0.0
    assert body["status"] == "needs_review"
    assert any("OCR" in w["message"] for w in body["warnings"])


def test_a_corrupt_pdf_is_told_apart_from_a_scan(client):
    """Both are unreadable, but only one is worth re-downloading."""
    token = verify_identity(client, sign_in(client))
    body = client.post(
        "/api/documents/w2/file",
        files={"file": ("broken.pdf", b"%PDF-1.4 truncated", "application/pdf")},
        data={"tax_year": "2025"},
        headers=auth(token),
    ).json()
    assert body["parse_confidence"] == 0.0
    assert any("corrupt or password-protected" in w["message"] for w in body["warnings"])


# ---------------------------------------------------------------------------
# the estimate
# ---------------------------------------------------------------------------
@pytest.fixture
def ready(client):
    """Signed in, verified, with one W-2 for 2025 on file."""
    token = verify_identity(client, sign_in(client))
    client.post("/api/documents/w2/boxes", json=W2_2025, headers=auth(token))
    return token


def test_regular_estimate_covers_federal_and_state(client, ready):
    response = client.post("/api/estimates", headers=auth(ready), json={
        "tax_year": 2025, "method": "regular",
        "situation": {"filing_status": "married_jointly", "resident_state": "CA",
                      "children_under_17": 2, "age": 43, "spouse_age": 41},
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["method"] == "regular"
    assert float(body["totals"]["federal_tax"]) > 0
    assert [s["code"] for s in body["states"]] == ["CA"]
    assert float(body["states"][0]["tax"]) > 0
    assert body["payment"]["direction"] in ("refund", "balance_due", "even")
    assert "estimate_id" in body


def test_no_tax_state_shows_zero_and_says_why(client):
    token = verify_identity(client, sign_in(client), state="TX")
    texan = {**W2_2025, "states": [{"state": "TX", "state_wages": 118000, "state_withheld": 0}]}
    client.post("/api/documents/w2/boxes", json=texan, headers=auth(token))
    body = client.post("/api/estimates", headers=auth(token), json={
        "tax_year": 2025, "method": "regular",
        "situation": {"filing_status": "single", "resident_state": "TX"},
    }).json()
    state = body["states"][0]
    assert state["code"] == "TX"
    assert float(state["tax"]) == 0
    assert "no individual income tax" in " ".join(state["notes"]).lower()


def test_planning_offers_moves_and_beats_the_regular_estimate(client, ready):
    body = client.post("/api/estimates", headers=auth(ready), json={
        "tax_year": 2025, "method": "planning",
        "situation": {"filing_status": "married_jointly", "resident_state": "CA",
                      "children_under_17": 2, "age": 43, "spouse_age": 41,
                      "existing_401k": 8000, "has_hdhp": True, "hdhp_family": True},
    }).json()
    assert body["method"] == "planning"
    assert body["strategies"], "planning mode must offer something"
    assert body["baseline"]["method"] == "regular"

    for strategy in body["strategies"]:
        assert strategy["label"] and strategy["how_it_works"]
        assert "closes_on" in strategy and "still_available" in strategy
    # Planning can never come out worse than filing as-is.
    assert float(body["totals"]["total_tax"]) <= float(body["baseline"]["totals"]["total_tax"])


def test_compare_shows_both_methods_side_by_side(client, ready):
    body = client.post("/api/estimates/compare", headers=auth(ready), json={
        "tax_year": 2025,
        "situation": {"filing_status": "single", "resident_state": "CA",
                      "has_hdhp": True, "existing_401k": 8000},
    }).json()
    assert set(body) == {"regular", "planning", "difference"}
    assert float(body["difference"]) >= 0


def test_an_estimate_without_income_says_so(client):
    token = verify_identity(client, sign_in(client))
    response = client.post("/api/estimates", headers=auth(token),
                           json={"tax_year": 2025, "method": "regular"})
    assert response.status_code == 400
    assert "no income on file" in response.json()["detail"]


def test_an_unsupported_year_is_refused_clearly(client, ready):
    response = client.post("/api/estimates", headers=auth(ready),
                           json={"tax_year": 1999, "method": "regular"})
    assert response.status_code == 400
    assert "not supported" in response.json()["detail"]


# ---------------------------------------------------------------------------
# prior years and payment
# ---------------------------------------------------------------------------
def test_filing_history_flags_the_unfiled_year(client, ready):
    client.post("/api/estimates", headers=auth(ready),
                json={"tax_year": 2025, "method": "regular",
                      "situation": {"filing_status": "single", "resident_state": "CA"}})
    body = client.get("/api/filings/history?years=2024,2025", headers=auth(ready)).json()
    assert body["taxpayer"]["ssn"] == "***-**-6789"
    assert 2025 in body["summary"]["unfiled_years"]
    assert "8821" in body["source_note"]

    recorded = client.post("/api/filings/record", headers=auth(ready), json={
        "tax_year": 2025, "jurisdiction": "federal", "state": "accepted",
        "filed_on": "2026-03-01",
    })
    assert recorded.status_code == 200
    assert recorded.json()["source"] == "client_stated"

    after = client.get("/api/filings/history?years=2025", headers=auth(ready)).json()
    federal = next(y for y in after["years"] if y["jurisdiction"] == "federal")
    assert federal["status"] == "accepted"
    assert federal["severity"] == "ok"


def test_payment_options_price_a_balance(client, ready):
    body = client.get("/api/payments/options?balance=9400&can_pay_in_full=false",
                      headers=auth(ready)).json()
    assert body["direction"] == "balance_due"
    methods = {o["method"] for o in body["options"]}
    assert {"irs_direct_pay", "short_term_extension"} <= methods
    plans = [o for o in body["options"] if o["instalments"] > 1 and o["available"]]
    assert plans, "a client who cannot pay in full must be offered a plan"
    for option in plans:
        assert float(option["total_cost"]) >= float(option["amount"])


def test_refund_options_lead_with_direct_deposit(client, ready):
    body = client.get("/api/payments/options?balance=-3200", headers=auth(ready)).json()
    assert body["direction"] == "refund"
    assert body["options"][0]["method"] == "direct_deposit"
    assert body["options"][0]["recommended"] is True


#: A household that lands on a refund, so the direct-deposit route applies.
REFUNDING = {"filing_status": "married_jointly", "resident_state": "CA",
             "children_under_17": 2, "age": 43, "spouse_age": 41}


def test_a_mistyped_routing_number_is_rejected(client, ready):
    estimate = client.post("/api/estimates", headers=auth(ready), json={
        "tax_year": 2025, "method": "regular", "situation": REFUNDING,
    }).json()
    assert float(estimate["totals"]["total_balance"]) < 0, "this case should refund"
    response = client.post("/api/payments/choose", headers=auth(ready), json={
        "estimate_id": estimate["estimate_id"], "method": "direct_deposit",
        "bank": {"routing_number": "123456789", "account_number": "000123456789"},
    })
    assert response.status_code == 400
    assert "checksum" in response.json()["detail"]


def test_bank_details_come_back_masked(client, ready):
    estimate = client.post("/api/estimates", headers=auth(ready), json={
        "tax_year": 2025, "method": "regular", "situation": REFUNDING,
    }).json()
    response = client.post("/api/payments/choose", headers=auth(ready), json={
        "estimate_id": estimate["estimate_id"], "method": "direct_deposit",
        # A real, checksum-valid routing number (Wells Fargo, 121000248).
        "bank": {"routing_number": "121000248", "account_number": "000123456789"},
    })
    assert response.status_code == 200, response.text
    assert response.json()["bank_account"] == "••••6789"
    assert "121000248" not in response.text


# ---------------------------------------------------------------------------
# isolation between accounts
# ---------------------------------------------------------------------------
def test_a_balance_due_offers_direct_debit_not_direct_deposit(client, ready):
    estimate = client.post("/api/estimates", headers=auth(ready), json={
        "tax_year": 2025, "method": "regular",
        "situation": {"filing_status": "single", "resident_state": "CA"},
    }).json()
    assert float(estimate["totals"]["total_balance"]) > 0, "this case should owe"
    response = client.post("/api/payments/choose", headers=auth(ready), json={
        "estimate_id": estimate["estimate_id"], "method": "direct_debit",
        "bank": {"routing_number": "121000248", "account_number": "000123456789"},
    })
    assert response.status_code == 200, response.text
    assert response.json()["direction"] == "balance_due"
    assert response.json()["bank_account"] == "••••6789"


def test_one_account_cannot_read_another_s_documents(client, ready):
    other = verify_identity(
        client, sign_in(client, "sam@example.com"),
        ssn="987-65-4320", email="sam@example.com", mobile="4155550199",
    )
    mine = client.get("/api/documents", headers=auth(ready)).json()["documents"]
    theirs = client.get("/api/documents", headers=auth(other)).json()["documents"]
    assert len(mine) == 1
    assert theirs == []

    stolen = client.get(f"/api/estimates/{1}", headers=auth(other))
    assert stolen.status_code == 404


def test_one_ssn_cannot_be_claimed_by_two_accounts(client, ready):
    token = sign_in(client, "impostor@example.com")
    started = client.post("/api/auth/mobile/start", json={"mobile": "4155550177"},
                          headers=auth(token))
    client.post("/api/auth/mobile/verify",
                json={"code": started.json()["development_code"]}, headers=auth(token))
    response = client.post("/api/auth/identity", headers=auth(token), json={
        "ssn": "123-45-6789", "email": "impostor@example.com", "mobile": "4155550177",
    })
    assert response.status_code == 400
    assert "already registered to a different account" in response.json()["detail"]


# ---------------------------------------------------------------------------
# reference data
# ---------------------------------------------------------------------------
def test_states_reference_splits_taxing_from_non_taxing(client):
    body = client.get("/api/reference/states").json()
    assert body["counts"]["total"] == 51
    assert body["counts"]["no_income_tax"] == 9
    assert set(body["no_income_tax"]) == {"AK", "FL", "NH", "NV", "SD", "TN", "TX", "WA", "WY"}
    california = next(s for s in body["states"] if s["code"] == "CA")
    assert california["type"] == "graduated" and california["has_income_tax"] is True
    texas = next(s for s in body["states"] if s["code"] == "TX")
    assert texas["has_income_tax"] is False


def test_security_headers_are_set(client):
    response = client.get("/api/health")
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "no-store" in response.headers["Cache-Control"]
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
