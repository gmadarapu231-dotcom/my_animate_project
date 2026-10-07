"""The agent, end to end, and the fee model it refuses to offer."""

from __future__ import annotations

from decimal import Decimal

import pytest

from taxvault.agent import Document, run_agent, sample_bundle
from taxvault.agent.pipeline import (
    FAILED,
    NEEDS_CLIENT_INPUT,
    READY_FOR_SIGNATURE,
)
from taxvault.agent.sandbox import SCENARIOS, SandboxUnavailable, scanned_pdf, w2_pdf
from taxvault.engines.commission import (
    ProhibitedFeeModel,
    describe_models,
    quote_platform_fee,
    refund_handling,
)

pytest.importorskip("reportlab", reason="the sandbox builds real PDFs")


def D(value):
    return Decimal(str(value))


def _run(key: str, year: int = 2025, **kwargs):
    case, documents = sample_bundle(key, year=year)
    return case, run_agent(
        documents, tax_year=year, taxpayer_name=case.taxpayer_name,
        filing_status=case.filing_status, resident_state=case.resident_state,
        situation=dict(case.situation), **kwargs,
    )


# ---------------------------------------------------------------------------
# every scenario gets through the pipeline
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key", sorted(SCENARIOS))
def test_every_scenario_runs_without_failing(key):
    _, run = _run(key)
    assert run.state != FAILED, [s.detail for s in run.steps if s.status == "blocked"]
    assert run.estimate is not None
    assert run.client_fee is not None


@pytest.mark.parametrize("key", sorted(SCENARIOS))
def test_every_scenario_records_every_step(key):
    _, run = _run(key)
    names = [s.name for s in run.steps]
    assert names == ["classify", "extract", "identity", "reconcile", "assemble",
                     "compute", "review", "price", "gate"]
    assert all(s.duration_ms >= 0 for s in run.steps)


# ---------------------------------------------------------------------------
# reading the documents
# ---------------------------------------------------------------------------
def test_a_single_w2_is_read_in_full():
    _, run = _run("simple")
    assert run.confidence == 1.0
    assert [d["kinds"] for d in run.documents] == [["w2"]]
    assert Decimal(run.estimate["federal"]["agi"]) == D("68000.00")


def test_a_consolidated_statement_yields_four_forms_from_three_files():
    _, run = _run("investor")
    kinds = sorted(k for d in run.documents for k in d["kinds"])
    assert kinds == ["1098", "1099_b", "1099_div", "w2"]


def test_schedule_d_nets_through_the_agent():
    """Short-term loss against long-term gain: the survivor stays long-term."""
    _, run = _run("investor")
    capital = run.estimate["federal"]["capital"]
    assert capital["short_term_net"] == "-5690.00"     # -6,600 plus 910 wash sale
    assert capital["long_term_net"] == "27390.00"      # 26,150 plus 1,240 distribution
    assert capital["ordinary_component"] == "0"
    assert capital["preferential_component"] == "21700.00"


def test_the_mortgage_ceiling_is_applied_through_the_agent():
    """$812,000 of debt against a $750,000 ceiling prorates the interest."""
    _, run = _run("investor")
    mortgage = run.estimate["federal"]["mortgage"]
    assert mortgage["total_interest"] == "31400.00"
    assert mortgage["deductible_interest"] == "29002.46"
    assert mortgage["allowed_debt"] == "750000.00"


def test_an_early_distribution_produces_the_ten_percent():
    _, run = _run("early_saver")
    assert Decimal(run.estimate["federal"]["early_withdrawal_penalty"]) == D("4200.00")


# ---------------------------------------------------------------------------
# what it refuses to guess
# ---------------------------------------------------------------------------
def test_a_scanned_document_blocks_rather_than_being_guessed_at():
    _, run = _run("unreadable")
    assert run.state == NEEDS_CLIENT_INPUT
    assert len(run.blockers) == 1
    assert "could not be read" in run.blockers[0].question
    assert run.confidence < 0.75          # a missing form is not 90% right


def test_an_unreadable_document_holds_the_filing_gate_shut():
    _, run = _run("unreadable")
    outstanding = {r["key"] for r in run.gate.outstanding}
    assert "documents_readable" in outstanding
    assert "no_blockers" in outstanding


def test_a_run_with_no_readable_document_fails_rather_than_computing():
    run = run_agent(
        [Document(filename="photo.pdf", content_type="application/pdf",
                  blob=scanned_pdf())],
        tax_year=2025, taxpayer_name="Sam Delacroix",
    )
    assert run.state == FAILED
    assert any(s.name == "extract" and s.status == "blocked" for s in run.steps)


def test_a_name_mismatch_is_a_blocker():
    """The IRS matches name and SSN before anything else; a mismatch rejects."""
    blob = w2_pdf(employer="Harbour Foods LLC", ein="94-1112223",
                  first="Sam", last="Delacroix", year=2025,
                  wages=52000, withheld=4900, state="TX")
    run = run_agent(
        [Document(filename="w2.pdf", content_type="application/pdf", blob=blob)],
        tax_year=2025, taxpayer_name="Jordan Whitfield",
    )
    assert run.blockers
    assert any("Which is right?" in item.question for item in run.blockers)


def test_two_w2s_from_one_employer_are_queried_not_summed_silently():
    blob = w2_pdf(employer="Fresno Produce Co", ein="94-1234567",
                  first="Alex", last="Rivera", year=2025,
                  wages=68000, withheld=7400, state="CA")
    run = run_agent(
        [Document(filename="a.pdf", content_type="application/pdf", blob=blob),
         Document(filename="b.pdf", content_type="application/pdf", blob=blob)],
        tax_year=2025, taxpayer_name="Alex Rivera",
    )
    assert any("uploaded twice" in item.question for item in run.blockers)


# ---------------------------------------------------------------------------
# the questions
# ---------------------------------------------------------------------------
def test_an_early_withdrawal_asks_about_the_exception_with_the_figure_attached():
    _, run = _run("early_saver")
    asked = [i for i in run.review if i.field == "distributions[].penalty_exception"]
    assert asked, [i.field for i in run.review]
    # A question with no number next to it gets skipped.
    assert asked[0].moves == "4,200"


def test_a_mortgage_asks_what_the_borrowing_paid_for():
    _, run = _run("investor")
    asked = [i for i in run.review if i.field == "loans[].used_for"]
    assert asked
    assert asked[0].moves == "29,002"


def test_answering_the_questions_moves_the_return_forward():
    case, first = _run("investor")
    assert first.state == NEEDS_CLIENT_INPUT

    _, second = _run(
        "investor",
        answered={item.field for item in first.open_questions if item.field},
        client_reviewed=True, client_signed_8879=True, preparer_ptin="P01234567",
    )
    assert second.state == READY_FOR_SIGNATURE
    assert second.open_questions == []
    # The questions stay on the record: what was asked and what came back is
    # part of the return, not something to discard once answered.
    assert len(second.review) == len(first.review)
    assert all(item.answered for item in second.review)


# ---------------------------------------------------------------------------
# the filing gate
# ---------------------------------------------------------------------------
def test_no_run_can_ever_transmit():
    """Every path ends at the EFIN, because that is an IRS authorisation."""
    for key in SCENARIOS:
        case, documents = sample_bundle(key, year=2025)
        run = run_agent(
            documents, tax_year=2025, taxpayer_name=case.taxpayer_name,
            filing_status=case.filing_status, resident_state=case.resident_state,
            situation=dict(case.situation),
            answered={"loans[].used_for", "sales.long_term", "filing_status",
                      "sales", "distributions[].penalty_exception", "ira_basis"},
            client_reviewed=True, client_signed_8879=True, preparer_ptin="P01234567",
        )
        assert run.gate.can_transmit is False, key
        outstanding = {r["key"] for r in run.gate.outstanding}
        assert "efin_transmitter" in outstanding, key


def test_the_efin_requirement_is_marked_as_not_a_software_problem():
    _, run = _run("simple")
    efin = next(r for r in run.gate.requirements if r["key"] == "efin_transmitter")
    assert efin["satisfiable_in_software"] is False
    assert "EFIN" in efin["detail"]


def test_an_unsigned_8879_holds_the_gate_shut():
    _, run = _run("simple", client_reviewed=True, preparer_ptin="P01234567")
    outstanding = {r["key"] for r in run.gate.outstanding}
    assert "form_8879" in outstanding


def test_software_is_not_a_preparer():
    _, run = _run("simple", client_reviewed=True, client_signed_8879=True)
    ptin = next(r for r in run.gate.requirements if r["key"] == "preparer_ptin")
    assert ptin["met"] is False
    assert "Software is not a preparer" in ptin["detail"]


# ---------------------------------------------------------------------------
# pricing
# ---------------------------------------------------------------------------
def test_the_price_follows_the_work_not_the_refund():
    """The investor return costs more than the simple one, and it is the work."""
    _, simple = _run("simple")
    _, investor = _run("investor")
    assert Decimal(investor.client_fee["total"]) > Decimal(simple.client_fee["total"])
    assert investor.client_fee["tier"] == "investment"


def test_a_refund_percentage_is_refused_with_the_citation():
    with pytest.raises(ProhibitedFeeModel) as caught:
        quote_platform_fee(model="refund_percentage", refund=D(4000))
    message = str(caught.value)
    assert "10.27" in message
    assert "contingent fee" in message
    assert "Instead:" in message


def test_passing_a_refund_to_a_legal_model_is_ignored_and_said_so():
    fee = quote_platform_fee(returns_this_period=10, refund=D(9000))
    assert fee.amount == D("12.00")
    assert any("ignored" in note for note in fee.notes)


def test_volume_pricing_steps_down():
    small = quote_platform_fee(returns_this_period=10).amount
    large = quote_platform_fee(returns_this_period=6000).amount
    assert large < small


def test_a_revenue_share_is_a_share_of_the_fee_not_the_refund():
    fee = quote_platform_fee(model="revenue_share", preparation_fee=D(325))
    assert fee.amount == D("48.75")           # 15% of 325
    assert fee.billed_to == "firm"


def test_a_revenue_share_is_floored_and_capped():
    tiny = quote_platform_fee(model="revenue_share", preparation_fee=D(30))
    huge = quote_platform_fee(model="revenue_share", preparation_fee=D(5000))
    assert tiny.amount == D("8.00")
    assert huge.amount == D("150.00")


def test_a_subscription_charges_nothing_extra_until_the_allowance_runs_out():
    inside = quote_platform_fee(model="subscription", returns_this_period=20)
    outside = quote_platform_fee(model="subscription", returns_this_period=200)
    assert inside.amount == D("0")
    assert outside.amount == D("6.00")


def test_a_fee_may_not_be_taken_out_of_a_refund():
    handling = refund_handling()
    assert handling["allowed"] is False
    assert "money transmitter" in handling["refusal"]


def test_the_prohibited_model_is_listed_so_nobody_proposes_it_twice():
    described = describe_models()
    refused = [m for m in described["models"] if not m["allowed"]]
    assert len(refused) == 1
    assert refused[0]["key"] == "refund_percentage"
    assert "10.27" in refused[0]["refusal"]
    assert refused[0]["instead"]


# ---------------------------------------------------------------------------
# through the API
# ---------------------------------------------------------------------------
def test_the_sandbox_endpoint_runs_the_whole_pipeline(client):
    response = client.post("/api/agent/sandbox/investor?year=2025")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run"]["state"] == NEEDS_CLIENT_INPUT
    assert body["run"]["estimate"]["federal"]["mortgage"]["deductible_interest"] == "29002.46"


def test_the_sandbox_can_show_the_answered_end_state(client):
    response = client.post(
        "/api/agent/sandbox/simple?year=2025&answer_everything=true"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["after_answers"]["state"] == READY_FOR_SIGNATURE
    assert body["after_answers"]["gate"]["outstanding"] == ["efin_transmitter"]


def test_an_unknown_scenario_is_a_404(client):
    assert client.post("/api/agent/sandbox/nonsense").status_code == 404


def test_the_pricing_endpoint_publishes_the_refusal(client):
    body = client.get("/api/agent/pricing").json()
    refused = {m["key"] for m in body["models"] if not m["allowed"]}
    assert refused == {"refund_percentage"}
    assert body["deduct_from_refund"]["allowed"] is False
    assert "not by your refund" in body["disclosure"]["on_every_quote"]


def test_the_agent_run_endpoint_needs_a_verified_client(client):
    response = client.post(
        "/api/agent/run",
        files={"files": ("w2.pdf", b"%PDF-1.4 not really", "application/pdf")},
    )
    assert response.status_code in (401, 403)


def test_a_verified_client_can_run_the_agent_on_an_upload(client):
    from tests.test_tax_journey import auth, sign_in, verify_identity

    token = verify_identity(client, sign_in(client))
    blob = w2_pdf(employer="Fresno Produce Co", ein="94-1234567",
                  first="Dana", last="Reed", year=2025,
                  wages=68000, withheld=7400, state="CA", state_withheld=2650)
    response = client.post(
        "/api/agent/run",
        files=[("files", ("w2.pdf", blob, "application/pdf"))],
        data={"tax_year": "2025", "filing_status": "single", "age": "40"},
        headers=auth(token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["estimate"]["federal"]["agi"] == "68000.00"
    assert body["gate"]["can_transmit"] is False
