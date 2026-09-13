"""The classifier must work for ANY profession, not just technology."""

from __future__ import annotations

import pytest

from careeros.ai.provider import get_provider
from careeros.engines.classifier import JobClassifier


@pytest.fixture
def classifier():
    return JobClassifier(provider=get_provider("off"))


SAP_JD = """Senior SAP Security Consultant - Contract
Required Skills:
- 7+ years of SAP Security and SAP GRC Access Control experience
- Hands-on PFCG role design, derived roles and composite roles
- Firefighter / Emergency Access Management
- Bachelor degree required
Preferred:
- SAP IAG exposure
- CISA certification
"""

HEALTHCARE_JD = """Clinical Data Analyst
Required Skills:
- 3+ years working with Epic or Cerner EHR data
- Strong SQL and Tableau
- Knowledge of HIPAA and HL7/FHIR standards
"""

FINANCE_JD = """Senior Financial Analyst
Required Skills:
- 5+ years of financial modeling and variance analysis
- Advanced Excel and SQL
- GAAP reporting and month-end close
- CPA or CFA preferred
"""

NOVEL_JD = """Veterinary Practice Manager
Required Skills:
- 5+ years managing a veterinary or medical clinic
- Staff scheduling, payroll coordination and inventory of pharmaceuticals
- Client communication and conflict resolution
Preferred:
- Practice management software (ezyVet, Cornerstone)
"""


@pytest.mark.parametrize(
    "title,jd,expected_domain",
    [
        ("Senior SAP Security Consultant", SAP_JD, "sap"),
        ("Clinical Data Analyst", HEALTHCARE_JD, "healthcare"),
        ("Senior Financial Analyst", FINANCE_JD, "finance"),
        ("Senior Network Security Engineer", "Design BGP and OSPF routing, Cisco ISE and firewall policy.", "networking"),
    ],
)
def test_domains_across_industries(classifier, title, jd, expected_domain):
    result = classifier.classify(title, jd)
    assert result.domain_id == expected_domain


def test_unknown_profession_is_not_force_fitted(classifier):
    """A job the taxonomy has never seen must fall through to `other`,
    not be shoehorned into the nearest technology bucket."""
    result = classifier.classify("Veterinary Practice Manager", NOVEL_JD)
    assert result.domain_id == "other"
    assert result.confidence <= 0.2


def test_ats_keywords_are_derived_from_the_posting(classifier):
    """No industry keyword list is hard-coded anywhere."""
    vet = classifier.classify("Veterinary Practice Manager", NOVEL_JD)
    joined = " ".join(vet.ats_keywords)
    assert "veterinary" in joined
    assert not any(tech in joined for tech in ("kubernetes", "splunk", "bgp"))

    sap = classifier.classify("Senior SAP Security Consultant", SAP_JD)
    assert any("sap" in k for k in sap.ats_keywords)


def test_requirement_sections_are_separated(classifier):
    result = classifier.classify("Senior SAP Security Consultant", SAP_JD)
    required = {s.skill_id for s in result.required_skills}
    preferred = {s.skill_id for s in result.preferred_skills}
    assert "sap_grc" in required
    assert not (required & preferred), "a skill must not be both required and preferred"


def test_experience_education_and_certifications(classifier):
    result = classifier.classify("Senior SAP Security Consultant", SAP_JD)
    assert result.min_experience_years == 7.0
    assert result.education_requirement == "bachelors"
    assert "cisa" in result.required_certifications


def test_seniority_detection(classifier):
    assert classifier.classify("Senior Network Engineer", "x").seniority == "senior"
    assert classifier.classify("Junior Network Engineer", "x").seniority == "entry"
    assert classifier.classify("Network Architect", "x").seniority == "principal"


def test_work_arrangement_detection(classifier):
    result = classifier.classify("Data Analyst", "This is a 100% remote position.")
    assert result.work_arrangement == "remote"
