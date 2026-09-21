"""The IRS reference screen.

One rule, and a test to hold it: every link goes to a government domain. A
"helpful" link to a lookalike site is how people lose a refund, and a tax
product pointing anywhere else has no business doing so.
"""

from __future__ import annotations

from urllib.parse import urlparse

import pytest

from taxvault.config import resources

#: The only hosts a tax product should ever send somebody to.
ALLOWED = ("irs.gov", "eftps.gov", "ssa.gov", "treasury.gov")


def every_link():
    for group in resources()["groups"]:
        for link in group["links"]:
            yield group["key"], link


def test_every_link_is_on_a_government_domain():
    for key, link in every_link():
        host = urlparse(link["url"]).netloc
        assert host.endswith(ALLOWED), f"{key}: {link['label']} -> {host}"


def test_every_link_is_https():
    for key, link in every_link():
        assert urlparse(link["url"]).scheme == "https", f"{key}: {link['label']}"


def test_every_link_has_a_label_and_no_duplicates():
    seen = set()
    for _key, link in every_link():
        assert link["label"].strip()
        assert link["url"] not in seen, f"duplicate: {link['url']}"
        seen.add(link["url"])


def test_the_groups_cover_what_a_client_actually_asks_for():
    keys = {group["key"] for group in resources()["groups"]}
    assert {"money", "records", "filing", "safety", "help"} <= keys


def test_the_refund_tracker_is_there_because_it_is_the_first_question():
    labels = [link["label"] for _key, link in every_link()]
    assert any("Where's My Refund" in label for label in labels)
    assert any("transcript" in label.lower() for label in labels)
    assert any("Identity Protection PIN" in label for label in labels)


def test_the_scam_guidance_names_the_tactics():
    note = next(link["note"] for _key, link in every_link()
                if "scams" in link["label"].lower())
    for tactic in ("post", "Zelle", "gift card", "never"):
        assert tactic.lower() in note.lower()


# ---------------------------------------------------------------------------
# through the API
# ---------------------------------------------------------------------------
def test_the_reference_is_public(client):
    """Somebody who cannot get past sign-in may still need Where's My Refund."""
    response = client.get("/api/reference/irs")
    assert response.status_code == 200
    assert response.json()["groups"]


def test_a_state_adds_its_own_revenue_department(client):
    body = client.get("/api/reference/irs?state_code=CA").json()
    state_group = next(g for g in body["groups"] if g["key"] == "state")
    assert "California" in state_group["label"]
    assert "ftb.ca.gov" in state_group["links"][0]["url"]


def test_a_no_tax_state_says_there_is_nothing_to_file(client):
    body = client.get("/api/reference/irs?state_code=TX").json()
    state_group = next(g for g in body["groups"] if g["key"] == "state")
    assert state_group["links"] == []
    assert "no individual income tax" in state_group["blurb"].lower()


def test_an_unknown_state_is_ignored_rather_than_failing(client):
    response = client.get("/api/reference/irs?state_code=ZZ")
    assert response.status_code == 200
    assert not any(g["key"] == "state" for g in response.json()["groups"])


def test_the_response_disclaims_touching_the_irs_account(client):
    note = client.get("/api/reference/irs").json()["note"]
    assert "never ask for your IRS credentials" in note
