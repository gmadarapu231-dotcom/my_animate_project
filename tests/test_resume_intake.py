"""Reading a résumé: parsing, and the invariant it must not break.

The parser is the only place in the system where career facts are inferred
rather than asserted by the user, so the tests that matter most here are the
ones proving nothing it produces is ever treated as verified.
"""

from __future__ import annotations

import zipfile
from datetime import date

import pytest

from careeros.db.models import Certification, Employer, EvidenceItem
from careeros.enums import EvidenceKind, VerificationState
from careeros.resume_intake import intake_file, intake_text, terms_from_resume, verify
from careeros.resume_intake.extract import ExtractionError, extract_text, normalise
from careeros.resume_intake.parse import (
    _split_employer_line,
    find_range,
    parse_month,
    parse_resume,
    section_of,
    split_sections,
)

RESUME = """Priya Raman
Senior Network Security Engineer
Austin, TX · priya.raman@example.com · +1 512 555 0134
linkedin.com/in/priyaraman · github.com/praman

SUMMARY
Network security engineer with 8 years across financial services and SaaS.

EXPERIENCE

Cobalt Bank — Senior Network Security Engineer — Austin, TX
Mar 2021 – Present
• Led a zero trust microsegmentation programme across 14 data centre sites, cutting lateral movement by 68%
• Redesigned BGP and OSPF routing for 4,200 branch devices with no unplanned outage
• Deployed Cisco ISE for 9000 users and integrated it with Splunk for SOC alerting

Orrin Systems, Network Engineer, Dallas, TX
06/2018 - 02/2021
• Built SD-WAN overlay across 60 retail sites, reducing MPLS spend by 35%
• Automated switch configuration with Python and Ansible, saving 12 hours a week

SKILLS
Routing & Switching: BGP, OSPF, VLAN design, MPLS
Security: Cisco ISE, Palo Alto, Zero Trust, SIEM, Splunk, incident response

CERTIFICATIONS
CCNP Enterprise — Cisco, 2021
CompTIA Security+ — 2022

EDUCATION
B.E. Electronics and Communication, Anna University, 2017
"""

#: A résumé from a field the taxonomy does not seed, to prove the parser is
#: structural rather than keyword-driven.
VETERINARY = """Dr Amara Okafor
Veterinary Practice Manager
amara@example.com

PROFESSIONAL EXPERIENCE
Riverbend Animal Hospital | Practice Manager | Portland, OR | Feb 2020 - Present
• Managed a team of 18 across two clinics, lifting client retention by 22%
• Rebuilt the surgical scheduling process, cutting wait times from 19 days to 6

Hillcrest Veterinary Group, Veterinary Technician, 2016 - 2020
• Assisted in 900 surgical procedures a year
"""


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------
def test_plain_text_is_read_and_normalised(tmp_path):
    path = tmp_path / "cv.txt"
    path.write_text(RESUME, encoding="utf-8")
    text = extract_text(path)
    assert "Cobalt Bank" in text
    assert "\r" not in text


def test_a_docx_is_read_without_any_dependency(tmp_path):
    """A .docx is a zip with XML inside, so stdlib is enough."""
    from xml.sax.saxutils import escape

    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    paragraphs = "".join(
        f"<w:p><w:r><w:t>{escape(line)}</w:t></w:r></w:p>"
        for line in RESUME.split("\n")
        if line.strip()
    )
    path = tmp_path / "cv.docx"
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr(
            "word/document.xml",
            f'<?xml version="1.0"?><w:document {ns}><w:body>{paragraphs}</w:body></w:document>',
        )
    text = extract_text(path)
    assert "Cobalt Bank" in text and "CCNP Enterprise" in text


def test_a_line_break_inside_a_docx_run_is_real_whitespace(tmp_path):
    """Dropping <w:br/> welds two bullets into one sentence."""
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = (
        "<w:p><w:r><w:t>Led BGP work</w:t></w:r><w:r><w:br/></w:r>"
        "<w:r><w:t>and OSPF redesign across fourteen separate sites</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Deployed Cisco ISE for nine thousand users across "
        "the estate and wired it into Splunk for alerting</w:t></w:r></w:p>"
    )
    path = tmp_path / "b.docx"
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {ns}><w:body>{body}</w:body></w:document>')
    assert "Led BGP work\nand OSPF" in extract_text(path)


@pytest.mark.parametrize(
    "name,content,expect",
    [
        ("old.doc", b"binary", "Save As"),
        ("weird.xyz", b"x" * 200, "Supported"),
        ("broken.docx", b"not a zip at all", "Word document"),
        ("tiny.txt", b"too short", "too little"),
    ],
)
def test_unreadable_files_say_what_to_do(tmp_path, name, content, expect):
    path = tmp_path / name
    path.write_bytes(content)
    with pytest.raises(ExtractionError, match=expect):
        extract_text(path)


def test_a_missing_pdf_dependency_names_the_fix(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_pypdf(name, *args, **kwargs):
        if name == "pypdf":
            raise ImportError("no pypdf")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pypdf)
    path = tmp_path / "cv.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    with pytest.raises(ExtractionError) as excinfo:
        extract_text(path)
    assert "pip install" in str(excinfo.value)
    assert ".docx" in str(excinfo.value)   # and the workaround


# ---------------------------------------------------------------------------
# dates
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "token,expected",
    [
        ("Mar 2021", date(2021, 3, 1)),
        ("March 2021", date(2021, 3, 1)),
        ("Sept 2019", date(2019, 9, 1)),
        ("03/2021", date(2021, 3, 1)),
        ("2021-03", date(2021, 3, 1)),
        ("2021", date(2021, 1, 1)),
        ("Present", None),
    ],
)
def test_resume_date_tokens(token, expected):
    assert parse_month(token) == expected


@pytest.mark.parametrize("line", [
    "Mar 2021 – Present",
    "Mar 2021 - Current",
    "03/2021 to present",
    "2021 — now",
])
def test_an_open_ended_range_is_marked_current(line):
    found = find_range(line)
    assert found is not None
    _, end, current = found
    assert current is True and end is None


def test_a_closed_range_keeps_both_ends():
    start, end, current = find_range("06/2018 - 02/2021")
    assert start == date(2018, 6, 1) and end == date(2021, 2, 1) and not current


# ---------------------------------------------------------------------------
# header lines: the layouts résumés actually use
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "line,employer,title,location",
    [
        ("Cobalt Bank — Senior Network Security Engineer — Austin, TX",
         "Cobalt Bank", "Senior Network Security Engineer", "Austin, TX"),
        ("Orrin Systems, Network Engineer, Dallas, TX",
         "Orrin Systems", "Network Engineer", "Dallas, TX"),
        ("Data Engineer | Sahyadri Technologies | Bengaluru, Karnataka | Jan 2022 - Present",
         "Sahyadri Technologies", "Data Engineer", "Bengaluru, Karnataka"),
        ("Analyst at Vertex Health (2019 - 2021), Remote",
         "Vertex Health", "Analyst", "Remote"),
        ("Northwind Systems | Staff Network Engineer | Remote",
         "Northwind Systems", "Staff Network Engineer", "Remote"),
        ("Riverbend Animal Hospital | Practice Manager | Portland, OR | Feb 2020 - Present",
         "Riverbend Animal Hospital", "Practice Manager", "Portland, OR"),
    ],
)
def test_employer_title_and_location_survive_every_layout(line, employer, title, location):
    assert _split_employer_line(line) == (employer, title, location)


def test_a_hyphenated_title_is_not_split_apart():
    _, title, _ = _split_employer_line("Acme Corp | Site-Reliability Engineer | Remote")
    assert title == "Site-Reliability Engineer"


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("line,expected", [
    ("EXPERIENCE", "experience"),
    ("Professional Experience", "experience"),
    ("WORK HISTORY", "experience"),
    ("Technical Skills", "skills"),
    ("CERTIFICATIONS", "certifications"),
    ("Education", "education"),
    ("Led a zero trust programme across 14 sites.", None),
])
def test_headings_are_recognised_by_shape_then_keyword(line, expected):
    assert section_of(line) == expected


def test_everything_before_the_first_heading_is_the_header():
    sections = split_sections(RESUME)
    assert "Priya Raman" in "\n".join(sections["header"])
    assert any("Cobalt Bank" in line for line in sections["experience"])


# ---------------------------------------------------------------------------
# whole-résumé parsing
# ---------------------------------------------------------------------------
def test_a_resume_parses_into_employers_evidence_and_skills():
    parsed = parse_resume(RESUME)

    assert parsed.full_name == "Priya Raman"
    assert parsed.email == "priya.raman@example.com"
    assert parsed.links["linkedin"] == "linkedin.com/in/priyaraman"
    assert parsed.current_title == "Senior Network Security Engineer"

    assert [e.name for e in parsed.employers] == ["Cobalt Bank", "Orrin Systems"]
    assert parsed.employers[0].current is True
    assert parsed.employers[0].start_date == date(2021, 3, 1)
    assert parsed.employers[1].end_date == date(2021, 2, 1)

    # Dates on their own line belong to the entry above them.
    assert all(e.start_date is not None for e in parsed.employers)
    assert parsed.total_experience_years > 7

    assert len(parsed.evidence) == 5
    assert {"routing_switching", "zero_trust", "splunk"} <= set(parsed.skills)
    assert not parsed.warnings


def test_a_bullet_with_a_number_is_an_achievement_and_keeps_the_metric():
    parsed = parse_resume(RESUME)
    lateral = next(e for e in parsed.evidence if "lateral movement" in e.text)
    assert lateral.kind == EvidenceKind.ACHIEVEMENT.value
    assert lateral.metrics == {"%": 68}
    assert lateral.employer_name == "Cobalt Bank"
    assert lateral.start_date == date(2021, 3, 1)

    plain = next(e for e in parsed.evidence if "Palo Alto firewall" in e.text or "no unplanned outage" in e.text)
    assert plain.kind == EvidenceKind.RESPONSIBILITY.value


def test_a_year_is_not_mistaken_for_a_metric():
    parsed = parse_resume(
        "EXPERIENCE\nAcme | Engineer | 2019 - 2021\n• Delivered the 2020 platform migration on time\n"
    )
    assert parsed.evidence[0].metrics == {}


def test_the_parser_is_structural_not_keyword_driven():
    """A field the taxonomy does not seed still parses."""
    parsed = parse_resume(VETERINARY)
    assert [e.name for e in parsed.employers] == [
        "Riverbend Animal Hospital",
        "Hillcrest Veterinary Group",
    ]
    assert parsed.current_title == "Veterinary Practice Manager"
    retention = next(e for e in parsed.evidence if "retention" in e.text)
    assert retention.metrics == {"%": 22}
    assert parsed.total_experience_years > 5


def test_what_was_not_found_is_reported_rather_than_invented():
    parsed = parse_resume("Just a name\n\nSUMMARY\nI am looking for work in a new field entirely.\n")
    assert parsed.employers == []
    assert any("No employment entries" in w for w in parsed.warnings)
    assert any("No bullet points" in w for w in parsed.warnings)


def test_an_undated_role_is_flagged_not_guessed():
    parsed = parse_resume("EXPERIENCE\nAcme Corp — Senior Engineer\n• Ran the platform team for a while\n")
    assert parsed.employers[0].start_date is None
    assert parsed.total_experience_years == 0.0
    assert any("no readable date range" in w for w in parsed.warnings)


# ---------------------------------------------------------------------------
# search terms come from the résumé
# ---------------------------------------------------------------------------
def test_search_terms_are_the_resumes_own_titles():
    terms = terms_from_resume(parse_resume(RESUME))
    assert terms[0] == "Senior Network Security Engineer"
    assert "Network Engineer" in terms


def test_search_terms_work_for_a_field_nobody_configured():
    terms = terms_from_resume(parse_resume(VETERINARY))
    assert "Veterinary Practice Manager" in terms
    assert "Practice Manager" in terms


def test_skills_fill_in_when_a_resume_has_few_titles():
    parsed = parse_resume(
        "EXPERIENCE\nAcme | Engineer | 2020 - Present\n"
        "• Built Terraform modules and Python tooling for Azure\n"
    )
    terms = terms_from_resume(parsed, limit=4)
    assert len(terms) > 1
    assert any(t.lower() in ("python", "terraform / iac", "microsoft azure") or "python" in t.lower()
               for t in terms), terms


# ---------------------------------------------------------------------------
# committing: the invariant
# ---------------------------------------------------------------------------
def test_committed_evidence_is_always_unverified(db, user):
    report = intake_text(db, user, RESUME, source="test résumé")
    assert report["intake"]["evidence_created"] > 0

    rows = db.query(EvidenceItem).filter(EvidenceItem.source == "test résumé").all()
    assert rows
    assert {row.verification for row in rows} == {VerificationState.UNVERIFIED.value}


def test_high_parser_confidence_still_does_not_verify(db, user):
    """Confidence is recorded as strength. It is not a route to verified."""
    intake_text(db, user, RESUME, source="conf test")
    rows = db.query(EvidenceItem).filter(EvidenceItem.source == "conf test").all()
    best = max(rows, key=lambda r: r.strength)
    assert best.strength >= 0.7
    assert best.verification == VerificationState.UNVERIFIED.value


def test_only_an_explicit_call_verifies_evidence(db, user):
    intake_text(db, user, RESUME, source="verify test")
    rows = db.query(EvidenceItem).filter(EvidenceItem.source == "verify test").all()
    changed = verify(db, user, [rows[0].id])
    assert changed == 1
    db.refresh(rows[0])
    assert rows[0].verification == VerificationState.VERIFIED.value


def test_the_agent_has_no_tool_that_could_verify_evidence():
    """The invariant is kept by absence, so assert the absence."""
    from careeros.agent.guards import FORBIDDEN_CAPABILITIES, assert_tool_surface_is_safe

    for capability in ("add_evidence", "verify_evidence", "edit_evidence", "delete_evidence"):
        assert capability in FORBIDDEN_CAPABILITIES

    # And the check that runs on every registry build would reject one.
    with pytest.raises(Exception):
        assert_tool_surface_is_safe(["list_jobs", "verify_evidence"])


def test_employers_certifications_and_profile_come_across(db, user):
    before = db.query(Employer).filter(Employer.user_id == user.id).count()
    intake_text(db, user, RESUME, source="rows test", overwrite_profile=True)

    employers = db.query(Employer).filter(Employer.user_id == user.id).all()
    assert len(employers) > before
    assert {"Cobalt Bank", "Orrin Systems"} <= {e.name for e in employers}

    certs = {c.name for c in db.query(Certification).filter(Certification.user_id == user.id).all()}
    assert "CCNP Enterprise" in certs

    assert user.current_title == "Senior Network Security Engineer"
    assert user.total_experience_years > 7


def test_re_uploading_the_same_resume_does_not_duplicate_anything(db, user):
    first = intake_text(db, user, RESUME, source="dupe test")
    second = intake_text(db, user, RESUME, source="dupe test")

    assert first["intake"]["evidence_created"] > 0
    assert second["intake"]["evidence_created"] == 0
    assert second["intake"]["evidence_skipped"] == first["intake"]["evidence_created"]
    assert second["intake"]["employers_matched"] == 2


def test_a_second_upload_does_not_overwrite_a_hand_corrected_profile(db, user):
    user.current_title = "Corrected By Hand"
    db.flush()
    intake_text(db, user, RESUME, source="no overwrite")
    assert user.current_title == "Corrected By Hand"


def test_intake_from_a_file_reports_what_it_read(db, user, tmp_path):
    path = tmp_path / "cv.txt"
    path.write_text(RESUME, encoding="utf-8")
    report = intake_file(db, user, path)
    assert report["file"]["name"] == "cv.txt"
    assert report["file"]["characters"] > 500
    assert report["search_terms"]
    assert "unverified" in report["intake"]["next_step"]


def test_a_damaged_docx_body_is_reported_not_leaked(tmp_path):
    """A ParseError escaping to the caller is an unreadable error message."""
    path = tmp_path / "damaged.docx"
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("word/document.xml", "<w:document><unclosed>")
    with pytest.raises(ExtractionError, match="damaged document body"):
        extract_text(path)
