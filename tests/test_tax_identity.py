"""Does the W-2 belong to the person on the account?

The IRS matches the name and Social Security number on a return against Social
Security Administration records. A mismatch is rejected, and it is the single
commonest reason an e-filed return bounces. So the check happens when the W-2
goes in, where it costs a minute to fix.

Matching is graded, not binary: people are registered as Robert and paid as
Bob, W-2s print middle initials, and married names appear on one document and
not another. Only the client can settle those, so the system reports rather
than blocks.
"""

from __future__ import annotations

import pytest

from taxvault.forms.identity_match import compare_names, compare_ssn, surname_of, tokens
from tests.test_tax_journey import W2_2025, auth, sign_in, verify_identity


# ---------------------------------------------------------------------------
# name comparison
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("form_first,form_last,verdict", [
    ("Dana", "Reed", "exact"),
    ("Dana J", "Reed", "exact"),              # middle initial on the form
    ("DANA", "REED", "exact"),                # payroll shouts
    ("Dana", "Reed Jr", "exact"),             # generational suffix
    ("Dana", "Reed-Santos", "exact"),         # surname gained by marriage
    ("Bob", "Reed", "surname_only"),          # nickname
    ("Maria", "Santos-Rivera", "mismatch"),   # a different person
])
def test_names_are_graded_not_matched_exactly(form_first, form_last, verdict):
    result = compare_names(
        registered_first="Dana", registered_last="Reed",
        form_first=form_first, form_last=form_last,
    )
    assert result.verdict == verdict


def test_accents_do_not_break_a_match():
    result = compare_names(
        registered_first="José", registered_last="Peña",
        form_first="Jose", form_last="Pena",
    )
    assert result.verdict == "exact"


def test_a_mismatch_says_what_happens_next():
    result = compare_names(
        registered_first="Dana", registered_last="Reed",
        form_first="Maria", form_last="Santos-Rivera",
    )
    assert result.severity == "error"
    assert result.matches is False
    assert "rejected by the IRS" in result.message
    assert "Maria Santos-Rivera" in result.message and "Dana Reed" in result.message


def test_an_unreadable_name_is_reported_not_guessed():
    result = compare_names(registered_first="Dana", registered_last="Reed")
    assert result.verdict == "unknown"
    assert result.severity == "info"


def test_a_one_word_name_is_treated_as_the_surname():
    assert surname_of("", "", "Prince") == "prince"
    assert surname_of("", "", "MARIA J SANTOS") == "santos"
    assert tokens("Mr. Dana J Reed Jr.") == ["dana", "reed"]


# ---------------------------------------------------------------------------
# SSN comparison
# ---------------------------------------------------------------------------
def test_a_full_ssn_is_compared_without_decrypting_anything(tax_env):
    from taxvault.crypto import ssn_index

    index = ssn_index("123-45-6789")
    assert compare_ssn("123-45-6789", registered_index=index,
                       registered_last4="6789").verdict == "exact"
    assert compare_ssn("987-65-4320", registered_index=index,
                       registered_last4="6789").verdict == "mismatch"


def test_a_masked_ssn_falls_back_to_the_last_four(tax_env):
    from taxvault.crypto import ssn_index

    index = ssn_index("123-45-6789")
    assert compare_ssn("***-**-6789", registered_index=index,
                       registered_last4="6789").verdict == "last4"
    result = compare_ssn("XXX-XX-1111", registered_index=index, registered_last4="6789")
    assert result.verdict == "mismatch"
    assert result.severity == "error"


def test_no_ssn_on_the_form_is_not_a_failure(tax_env):
    assert compare_ssn("", registered_index="x", registered_last4="6789").verdict == "unknown"


# ---------------------------------------------------------------------------
# through the API
# ---------------------------------------------------------------------------
def test_a_matching_w2_raises_nothing(client):
    token = verify_identity(client, sign_in(client))   # registers Dana Reed
    body = client.post("/api/documents/w2/boxes", json=W2_2025, headers=auth(token)).json()
    assert body["identity_checks"] == []
    assert body["status"] == "parsed"


def test_somebody_elses_w2_is_flagged_and_held_for_review(client):
    token = verify_identity(client, sign_in(client))
    stranger = {**W2_2025, "employee_first_name": "Maria", "employee_last_name": "Santos-Rivera"}
    body = client.post("/api/documents/w2/boxes", json=stranger, headers=auth(token)).json()

    check = next(c for c in body["identity_checks"] if c["check"] == "name")
    assert check["severity"] == "error"
    assert "Maria Santos-Rivera" in check["message"]
    assert body["status"] == "needs_review"


def test_a_nickname_warns_without_holding_the_document(client):
    token = verify_identity(client, sign_in(client))
    nickname = {**W2_2025, "employee_first_name": "Bob"}
    body = client.post("/api/documents/w2/boxes", json=nickname, headers=auth(token)).json()

    check = next(c for c in body["identity_checks"] if c["check"] == "name")
    assert check["severity"] == "warning"
    assert body["status"] == "parsed"     # a warning is not a blocker


def test_the_account_name_can_be_corrected_to_the_one_on_the_w2(client):
    """When the W-2 is right and the sign-up was wrong, the account changes."""
    token = verify_identity(client, sign_in(client))
    stranger = {**W2_2025, "employee_first_name": "Dana", "employee_last_name": "Reed-Santos"}
    first = client.post("/api/documents/w2/boxes", json=stranger, headers=auth(token)).json()

    updated = client.patch("/api/auth/name", headers=auth(token),
                           json={"first_name": "Dana", "last_name": "Reed-Santos"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "Dana Reed-Santos"

    session = client.get("/api/auth/session", headers=auth(token)).json()
    assert session["taxpayer"]["name"] == "Dana Reed-Santos"

    # Re-saving the document re-runs the check against the corrected name.
    again = client.patch(f"/api/documents/{first['id']}", headers=auth(token), json={
        **stranger, "tax_year": 2025,
    })
    assert again.status_code == 200
    assert [c for c in again.json()["identity_checks"] if c["severity"] == "error"] == []


def test_an_empty_name_change_is_refused(client):
    token = verify_identity(client, sign_in(client))
    response = client.patch("/api/auth/name", headers=auth(token),
                            json={"first_name": "  ", "last_name": ""})
    assert response.status_code == 400


def test_a_name_change_is_written_to_the_audit_trail(client, tax_db):
    from sqlalchemy import select

    from taxvault.db.models import AuditEvent

    token = verify_identity(client, sign_in(client))
    client.patch("/api/auth/name", headers=auth(token),
                 json={"first_name": "Dana", "last_name": "Reed-Santos"})

    events = tax_db.scalars(
        select(AuditEvent).where(AuditEvent.action == "name_changed")
    ).all()
    assert events, "an identity change must be recorded"
    assert events[-1].detail["was"] == "Dana Reed"
    assert events[-1].detail["now"] == "Dana Reed-Santos"


def test_a_w2_for_a_different_ssn_is_flagged(client):
    """The W-2's own box a is checked against the account's number."""
    from taxvault.forms.w2_layout import read_w2_layout
    from tests.test_tax_upload import build_grid_w2_pdf

    token = verify_identity(client, sign_in(client), ssn="987-65-4320")
    # The sample PDF carries 123-45-6789 in box a, which is not this account's.
    form, _, _ = read_w2_layout(build_grid_w2_pdf())
    assert form.employee_ssn.replace("-", "") == "123456789"

    body = client.post(
        "/api/documents/w2/file",
        files={"file": ("w2.pdf", build_grid_w2_pdf(), "application/pdf")},
        data={"tax_year": "2025"},
        headers=auth(token),
    ).json()
    check = next(c for c in body["identity_checks"] if c["check"] == "ssn")
    assert check["severity"] == "error"
    assert body["status"] == "needs_review"


def test_the_ssn_read_off_a_form_is_never_stored(client):
    """It exists to be checked, then it goes. A document stores figures."""
    from tests.test_tax_upload import build_grid_w2_pdf

    token = verify_identity(client, sign_in(client))
    body = client.post(
        "/api/documents/w2/file",
        files={"file": ("w2.pdf", build_grid_w2_pdf(), "application/pdf")},
        data={"tax_year": "2025"},
        headers=auth(token),
    )
    assert "123-45-6789" not in body.text
    assert "123456789" not in body.text
    assert body.json()["payload"].get("employee_ssn", "") == ""
