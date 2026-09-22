"""ATS alignment: the posting's vocabulary, only for claims the evidence backs.

This module is where keyword stuffing would live if it lived anywhere, so most
of these tests are about what it refuses to write.
"""

from __future__ import annotations

import pytest

from careeros.engines.ats_align import (
    AlignmentPlan,
    AtsAligner,
    KeywordVerdict,
    is_presentable,
)
from careeros.engines.resume import EvidenceRecord, Resume, ResumeBlock, ResumeEntry, ResumeSection


def evidence(**overrides) -> EvidenceRecord:
    base = {
        "id": 1,
        "kind": "achievement",
        "text": "Led a zero trust microsegmentation programme across 14 sites",
        "technologies": ["Palo Alto", "Cisco ISE"],
        "skills": ["zero_trust", "network_security"],
        "role_title": "Senior Network Security Engineer",
        "verified": True,
    }
    base.update(overrides)
    return EvidenceRecord(**base)


def resume_with(*texts: str) -> Resume:
    document = Resume(name="test", kind="tailored")
    document.sections.append(
        ResumeSection(
            name="Skills",
            kind="skills",
            entries=[ResumeEntry(blocks=[ResumeBlock(text="Networking: BGP, OSPF", evidence_ids=[1])])],
        )
    )
    document.sections.append(
        ResumeSection(
            name="Professional Experience",
            kind="experience",
            entries=[
                ResumeEntry(
                    heading="Cobalt Bank",
                    blocks=[ResumeBlock(text=t, evidence_ids=[1]) for t in texts],
                )
            ],
        )
    )
    return document


def skills_text(document: Resume) -> str:
    section = next(s for s in document.sections if s.kind == "skills")
    return " | ".join(b.text for e in section.entries for b in e.blocks)


# ---------------------------------------------------------------------------
# the four verdicts
# ---------------------------------------------------------------------------
def test_a_keyword_already_in_the_resume_is_left_alone():
    document = resume_with("Owned Palo Alto firewall policy")
    plan = AtsAligner().align(document, [evidence()], ["palo alto"])
    assert [d.verdict for d in plan.decisions] == [KeywordVerdict.PRESENT]
    assert plan.added == []
    assert "palo alto" not in skills_text(document).lower().replace("Also stated", "")


def test_a_supported_keyword_is_added_in_the_postings_wording():
    """Evidence says "Cisco ISE"; the posting says "cisco identity services"."""
    document = resume_with("Deployed Cisco ISE for 9000 users")
    item = evidence(text="Deployed Cisco ISE for 9000 users", skills=["network_security"])
    plan = AtsAligner().align(document, [item], ["cisco ise"])
    decision = plan.decisions[0]
    assert decision.verdict is KeywordVerdict.PRESENT  # verbatim already


def test_an_unsupported_keyword_is_never_written_and_becomes_a_gap():
    document = resume_with("Owned Palo Alto firewall policy")
    plan = AtsAligner().align(document, [evidence()], ["sap firefighter"])
    decision = plan.decisions[0]
    assert decision.verdict is KeywordVerdict.UNSUPPORTED
    assert plan.gaps == ["sap firefighter"]
    assert "firefighter" not in skills_text(document).lower()
    assert "the fix is the experience" in decision.note


def test_the_gap_message_says_get_the_experience_not_type_the_word():
    plan = AtsAligner().align(resume_with("x" * 40), [evidence()], ["kubernetes"])
    assert "not the word" in plan.decisions[0].note


@pytest.mark.parametrize(
    "keyword",
    [
        "bachelor s degree",
        "master's degree",
        "b.tech",
        "security clearance",
        "top secret",
        "transfer h1b visas",
        "green card",
        "will sponsor",
        "us citizen",
        "equal opportunity employer",
    ],
)
def test_education_visa_and_clearance_keywords_are_never_written(keyword):
    document = resume_with("Owned Palo Alto firewall policy")
    plan = AtsAligner().align(document, [evidence()], [keyword])
    assert plan.decisions[0].verdict is KeywordVerdict.NOT_A_CLAIM
    assert plan.added == []
    assert keyword.split()[0] not in skills_text(document).lower() or "networking" in skills_text(document).lower()


@pytest.mark.parametrize(
    "keyword",
    [
        "strong communication skills",
        "years of experience",
        "excellent team player",
        "exceptional candidates.",
        "competitive compensation",
        "technical field",
    ],
)
def test_requirement_grammar_is_not_a_capability(keyword):
    plan = AtsAligner().align(resume_with("x" * 40), [evidence()], [keyword])
    assert plan.decisions[0].verdict is KeywordVerdict.NOT_A_CLAIM


@pytest.mark.parametrize(
    "keyword,presentable",
    [
        ("cisco asa", True),
        ("vlan design", True),
        ("zero trust", True),
        ("sd-wan migration", True),
        ("asa and cisco", False),          # ran across a connective
        ("ospf and vlan", False),
        ("deep hands-on bgp", False),      # leading filler
        ("own firewall policy", False),
        ("bank s zero", False),            # possessive split off
        ("administration of palo alto firewalls", False),   # a clause
    ],
)
def test_n_gram_artifacts_are_recognised(keyword, presentable):
    assert is_presentable(keyword) is presentable


def test_a_fragment_is_neither_written_nor_scored():
    document = resume_with("Owned Palo Alto firewall policy")
    plan = AtsAligner().align(document, [evidence()], ["asa and cisco"])
    decision = plan.decisions[0]
    assert decision.verdict is KeywordVerdict.FRAGMENT
    assert not decision.verdict.counts_for_ats
    assert plan.added == []
    assert "and cisco" not in skills_text(document).lower()
    assert "gibberish" in decision.note


# ---------------------------------------------------------------------------
# what gets printed
# ---------------------------------------------------------------------------
def test_several_n_grams_for_one_capability_print_once_as_the_cleanest_term():
    document = resume_with("Ran the estate's firewalls and segmentation work daily")
    item = evidence(
        text="Ran the estate's firewalls and segmentation work daily",
        technologies=["Palo Alto"],
        skills=["network_security"],
    )
    plan = AtsAligner().align(
        document,
        [item],
        ["palo alto", "administration palo alto", "firewall administration palo"],
    )
    assert plan.added == ["palo alto"], plan.added
    assert skills_text(document).count("palo alto") == 1


def test_the_added_line_cites_the_evidence_behind_it():
    document = resume_with("Ran the estate's firewalls and segmentation work daily")
    item = evidence(id=42, technologies=["Palo Alto"], skills=["network_security"],
                    text="Ran the estate's firewalls and segmentation work daily")
    AtsAligner().align(document, [item], ["palo alto"])
    section = next(s for s in document.sections if s.kind == "skills")
    added = [b for e in section.entries for b in e.blocks if "Also stated" in b.text]
    assert added
    assert added[0].evidence_ids == [42]


def test_alignment_can_be_reported_without_changing_the_resume():
    document = resume_with("Ran the estate's firewalls daily for years")
    before = skills_text(document)
    plan = AtsAligner().align(
        document, [evidence(technologies=["Palo Alto"], skills=["network_security"])],
        ["palo alto"], apply=False,
    )
    assert skills_text(document) == before
    assert plan.added == ["palo alto"]     # what it *would* add
    assert any("would be added" in n for n in plan.notes)


def test_a_resume_with_no_skills_section_gets_one():
    document = Resume(name="bare", kind="tailored")
    document.sections.append(
        ResumeSection(name="Professional Summary", kind="summary",
                      entries=[ResumeEntry(blocks=[ResumeBlock(text="Engineer.", evidence_ids=[1])])])
    )
    AtsAligner().align(
        document, [evidence(technologies=["Palo Alto"], skills=["network_security"])], ["palo alto"]
    )
    assert any(s.kind == "skills" for s in document.sections)


# ---------------------------------------------------------------------------
# coverage
# ---------------------------------------------------------------------------
def test_coverage_counts_only_terms_a_real_ats_would_match():
    document = resume_with("Owned Palo Alto firewall policy")
    plan = AtsAligner().align(
        document,
        [evidence()],
        ["palo alto", "sap firefighter", "bachelor s degree", "asa and cisco"],
    )
    scored = {d.keyword for d in plan.scored}
    assert scored == {"palo alto", "sap firefighter"}
    assert plan.coverage == 50.0


def test_coverage_is_a_hundred_when_a_posting_yields_no_real_keywords():
    plan = AtsAligner().align(resume_with("x" * 40), [evidence()], ["bachelor s degree"])
    assert plan.coverage == 100.0
    assert plan.scored == []


def test_a_plan_serialises_for_the_api():
    plan = AtsAligner().align(
        resume_with("Owned Palo Alto firewall policy"), [evidence()],
        ["palo alto", "sap firefighter"],
    )
    payload = plan.to_dict()
    assert payload["coverage"] == 50.0
    assert payload["counts"]["unsupported"] == 1
    assert payload["gaps"] == ["sap firefighter"]
    assert len(payload["decisions"]) == 2


# ---------------------------------------------------------------------------
# alignment against the whole pipeline
# ---------------------------------------------------------------------------
def test_tailoring_lifts_the_ats_score_and_the_resume_still_passes_the_gate(db, user, pipeline):
    """The point of the whole exercise: a real lift, with the gate intact."""
    from careeros.db.models import AtsAssessment, Job
    from careeros.sources.jsonfile import JsonFileSource

    pipeline.ingest([JsonFileSource("data/sample_jobs.json")])
    pipeline.classify_jobs()
    pipeline.assess_for_user(user)
    db.flush()

    job = db.query(Job).filter(Job.company == "Cobalt Bank").first()
    baseline = db.query(AtsAssessment).filter(AtsAssessment.job_id == job.id).first()
    assert baseline is not None

    document, report = pipeline.tailor_for_job(user, job, use_ai=False)
    db.flush()

    tailored = (
        db.query(AtsAssessment)
        .filter(AtsAssessment.job_id == job.id, AtsAssessment.resume_id == document.id)
        .first()
    )
    assert tailored is not None, "tailoring must re-score against the posting"
    assert tailored.keyword_match > baseline.keyword_match
    assert tailored.overall > baseline.overall

    # And the factuality gate still passes, which is the whole constraint.
    assert report.passed
    assert document.is_final
    assert any("ATS keyword coverage" in n for n in document.tailoring_notes)


def test_alignment_never_puts_an_unsupported_claim_past_the_gate(db, user, pipeline):
    """If alignment ever added something unsupported, factuality would catch it."""
    from careeros.db.models import Job
    from careeros.sources.jsonfile import JsonFileSource

    pipeline.ingest([JsonFileSource("data/sample_jobs.json")])
    pipeline.classify_jobs()
    pipeline.assess_for_user(user)
    db.flush()

    for job in db.query(Job).all():
        if job.classification is None:
            continue
        document, report = pipeline.tailor_for_job(user, job, use_ai=False)
        db.flush()
        unsupported = [c for c in report.claims if c.verdict.value == "unsupported"]
        assert not unsupported, (
            f"job {job.id} produced unsupported claims: {[c.text for c in unsupported]}"
        )


def test_the_keywords_come_from_the_posting_not_from_an_industry_list(db, user, pipeline):
    from careeros.db.models import Job
    from careeros.sources.jsonfile import JsonFileSource

    pipeline.ingest([JsonFileSource("data/sample_jobs.json")])
    pipeline.classify_jobs()
    db.flush()

    by_company = {j.company: j for j in db.query(Job).all() if j.classification}
    network = set(by_company["Cobalt Bank"].classification.ats_keywords)
    vet = set(by_company["Brightpaw Veterinary Group"].classification.ats_keywords)

    assert network and vet
    # Two postings in unrelated fields must not share a keyword vocabulary.
    assert len(network & vet) <= 2, sorted(network & vet)


def test_the_packet_carries_its_own_ats_standing(db, user, pipeline):
    from careeros.apply.channels import detect
    from careeros.apply.packet import build
    from careeros.db.models import AtsAssessment, Job
    from careeros.sources.jsonfile import JsonFileSource

    pipeline.ingest([JsonFileSource("data/sample_jobs.json")])
    pipeline.classify_jobs()
    pipeline.assess_for_user(user)
    db.flush()
    job = db.query(Job).filter(Job.company == "Cobalt Bank").first()
    document, _ = pipeline.tailor_for_job(user, job, use_ai=False)
    db.flush()
    ats = (
        db.query(AtsAssessment)
        .filter(AtsAssessment.job_id == job.id, AtsAssessment.resume_id == document.id)
        .first()
    )

    packet = build(
        user=user, job=job, channel=detect(url=job.url), resume=document, ats=ats
    )
    assert packet.ats_overall == ats.overall
    assert packet.ats_keyword_match == ats.keyword_match
    assert packet.ats_matched
    payload = packet.to_dict()
    assert payload["ats"]["keyword_match"] == ats.keyword_match
