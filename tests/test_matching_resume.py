"""Cross-domain matching, evidence-based resumes, and the factuality gate.

These are the tests that encode the product's central safety promise:
*the system may present transferable experience, but it may never claim
experience the user does not have.*
"""

from __future__ import annotations

from datetime import date

import pytest

from careeros.ai.provider import get_provider
from careeros.engines.classifier import JobClassifier
from careeros.engines.factuality import FactualityChecker
from careeros.engines.matching import EvidenceRef, SkillMatcher
from careeros.engines.resume import ContactInfo, EvidenceRecord, ResumeBuilder, render_text
from careeros.enums import MatchKind

CERTS = [{"name": "CCNP Enterprise", "issuer": "Cisco"}]
EDUCATION = [{"degree": "Bachelor of Science", "field": "Information Systems",
              "institution": "UT Austin", "completed_on": "2015"}]


@pytest.fixture
def evidence():
    return [
        EvidenceRecord(
            1, "achievement",
            "Reduced incident resolution time by 35% by building Splunk dashboards for 30 sites",
            employer="Acme Corp", role_title="Senior Network Engineer", technologies=["Splunk"],
            start_date=date(2021, 3, 1), metrics={"reduction_pct": 35, "sites": 30},
            verified=True, strength=0.9,
        ),
        EvidenceRecord(
            2, "responsibility",
            "Administered Active Directory and Azure AD for 4,000 users including RBAC role assignment",
            employer="Acme Corp", role_title="Senior Network Engineer",
            start_date=date(2021, 3, 1), metrics={"users": 4000}, verified=True, strength=0.8,
        ),
        EvidenceRecord(
            3, "responsibility",
            "Ran quarterly user access reviews and produced audit evidence for SOX testing",
            employer="Acme Corp", role_title="Senior Network Engineer", verified=True, strength=0.6,
        ),
    ]


@pytest.fixture
def profile(evidence):
    matcher = SkillMatcher()
    refs = [
        EvidenceRef(e.id, e.text, e.skills, e.technologies, e.role_title, e.employer,
                    e.strength, e.verified)
        for e in evidence
    ]
    return matcher.build_profile(refs, certifications=["CCNP"], total_experience_years=8)


@pytest.fixture
def classifier():
    return JobClassifier(provider=get_provider("off"))


def _match(classifier, profile, jd, title="Engineer"):
    classification = classifier.classify(title, jd)
    return SkillMatcher().match(classification, profile, job_title=title)


def test_broad_requirement_is_evidenced_by_a_narrower_skill(classifier, profile):
    """JD says "Identity and Access Management"; the resume says Active
    Directory / Azure AD / RBAC. That is real, claimable experience."""
    result = _match(classifier, profile, "Required Skills:\n- Identity and Access Management (IAM)\n")
    iam = next(m for m in result.matches if "identity" in m.requirement)
    assert iam.kind is MatchKind.PARTIAL
    assert iam.claimable is True
    assert iam.via_skill_id in {"active_directory", "azure_ad", "rbac"}


def test_adjacent_domain_is_related_and_never_claimable(classifier, profile):
    """JD says "SAP GRC Access Control"; the resume has access governance work.
    Relevant - but the resume must not present it as SAP experience."""
    result = _match(classifier, profile, "Required Skills:\n- SAP GRC Access Control configuration\n")
    sap = next(m for m in result.matches if "sap" in m.requirement)
    assert sap.kind in {MatchKind.RELATED, MatchKind.MISSING}
    assert sap.claimable is False


def test_absent_skill_is_reported_as_a_gap(classifier, profile):
    result = _match(classifier, profile, "Required Skills:\n- Splunk SIEM\n- Kubernetes administration\n")
    assert "kubernetes" in " ".join(result.gaps).lower()


def test_plural_surface_forms_resolve(classifier, profile):
    """"user access reviews" must reach the `access certification` node."""
    assert profile.has("access_certification")


def test_master_resume_passes_factuality(evidence):
    contact = ContactInfo("Jane Roe", "jane@example.com")
    resume = ResumeBuilder(provider=get_provider("off")).build(
        contact=contact, evidence=evidence, certifications=CERTS, education=EDUCATION,
        total_years=8, emphasise=["splunk"],
    )
    report = FactualityChecker().check(resume, evidence, CERTS, EDUCATION)
    assert report.passed, report.summary
    assert report.unsupported_count == 0


def test_invented_technology_is_blocked(evidence):
    contact = ContactInfo("Jane Roe", "jane@example.com")
    builder = ResumeBuilder(provider=get_provider("off"))
    resume = builder.build(contact=contact, evidence=evidence, certifications=CERTS,
                           education=EDUCATION, total_years=8)
    block = next(b for s in resume.sections if s.kind == "experience"
                 for e in s.entries for b in e.blocks)
    block.original_text, block.rewritten = block.text, True
    block.text = "Led a Kubernetes migration across the estate"

    report = FactualityChecker().check(resume, evidence, CERTS, EDUCATION)
    assert not report.passed
    assert any("kubernetes" in i.lower() for c in report.unsupported for i in c.issues)


def test_invented_metric_is_blocked(evidence):
    contact = ContactInfo("Jane Roe", "jane@example.com")
    resume = ResumeBuilder(provider=get_provider("off")).build(
        contact=contact, evidence=evidence, certifications=CERTS, education=EDUCATION, total_years=8
    )
    block = next(b for s in resume.sections if s.kind == "experience"
                 for e in s.entries for b in e.blocks)
    block.original_text, block.rewritten = block.text, True
    block.text = "Reduced incident resolution time by 90% by building Splunk dashboards for 30 sites"

    report = FactualityChecker().check(resume, evidence, CERTS, EDUCATION)
    assert not report.passed
    assert any("90" in i for c in report.unsupported for i in c.issues)


def test_clearance_and_visa_language_is_always_blocked(evidence):
    contact = ContactInfo("Jane Roe", "jane@example.com")
    resume = ResumeBuilder(provider=get_provider("off")).build(
        contact=contact, evidence=evidence, certifications=CERTS, education=EDUCATION, total_years=8
    )
    block = next(b for s in resume.sections if s.kind == "experience"
                 for e in s.entries for b in e.blocks)
    block.text += " while holding an active Top Secret clearance"
    report = FactualityChecker().check(resume, evidence, CERTS, EDUCATION)
    assert not report.passed


def test_unheld_certification_is_blocked(evidence):
    contact = ContactInfo("Jane Roe", "jane@example.com")
    resume = ResumeBuilder(provider=get_provider("off")).build(
        contact=contact, evidence=evidence,
        certifications=[{"name": "CISSP"}],           # rendered onto the resume
        education=EDUCATION, total_years=8,
    )
    report = FactualityChecker().check(resume, evidence, CERTS, EDUCATION)  # but not in the profile
    assert not report.passed
    assert any("CISSP".lower() in i.lower() for c in report.unsupported for i in c.issues)


def test_tailoring_does_not_claim_a_missing_domain(evidence, classifier, profile):
    """The headline case: tailoring a networking background to an SAP job must
    not produce a resume that mentions SAP."""
    jd = ("Required Skills:\n- 7+ years SAP Security and SAP GRC Access Control\n"
          "- PFCG role design\n- Firefighter / Emergency Access Management\n")
    classification = classifier.classify("Senior SAP Security Consultant", jd)
    match = SkillMatcher().match(classification, profile, job_title="Senior SAP Security Consultant")

    contact = ContactInfo("Jane Roe", "jane@example.com")
    resume = ResumeBuilder(provider=get_provider("off")).tailor(
        contact=contact, evidence=evidence, classification=classification, match=match,
        job_title="Senior SAP Security Consultant", company="Halden",
        certifications=CERTS, education=EDUCATION, total_years=8, use_ai=False,
    )
    text = render_text(resume, contact).lower()
    assert "sap" not in text
    assert "pfcg" not in text
    assert any("unclaimed" in n.lower() for n in resume.notes)
    assert FactualityChecker().check(resume, evidence, CERTS, EDUCATION).passed


def test_headline_is_not_the_job_title_unless_actually_held(evidence):
    builder = ResumeBuilder(provider=get_provider("off"))
    assert builder._honest_headline("Chief Technology Officer", evidence) == "Senior Network Engineer"
    assert builder._honest_headline("Senior Network Engineer", evidence) == "Senior Network Engineer"


def test_every_experience_bullet_cites_evidence(evidence):
    contact = ContactInfo("Jane Roe", "jane@example.com")
    resume = ResumeBuilder(provider=get_provider("off")).build(
        contact=contact, evidence=evidence, certifications=CERTS, education=EDUCATION, total_years=8
    )
    for section in resume.sections:
        if section.kind not in {"experience", "summary", "skills"}:
            continue
        for entry in section.entries:
            for block in entry.blocks:
                assert block.evidence_ids, f"uncited block in {section.kind}: {block.text}"
