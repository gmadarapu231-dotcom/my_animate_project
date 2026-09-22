"""Capture real output from the new subsystems into the demo fixtures."""
import json
import os
import tempfile
from pathlib import Path

os.environ.update(
    CAREEROS_HOME=tempfile.mkdtemp(),
    CAREEROS_DATABASE_URL=f"sqlite:///{tempfile.mkdtemp()}/cap.db",
    CAREEROS_LLM="off",
)

RESUME = Path(
    "/tmp/claude-0/-home-user-my-animate-project/85ce3a30-ce64-565c-9487-7b9c1d1122ad"
    "/scratchpad/sample_resume.txt"
)

from careeros.db.session import init_db, new_session

init_db()
session = new_session()

from careeros.profile_io import load_profile

user = load_profile(session, "data/sample_profile.yaml")
session.commit()

# --- 1. résumé intake -------------------------------------------------------
from careeros.resume_intake import intake_file

intake = intake_file(session, user, RESUME, commit=True)
session.commit()

# --- 2. application channels ------------------------------------------------
from careeros.apply.channels import detect

CHANNEL_CASES = [
    ("https://boards.greenhouse.io/cobaltbank/jobs/5550001", "", "Greenhouse"),
    ("https://jobs.lever.co/northwind/9c1f2e3a", "", "Lever"),
    ("https://careers.example.com/req/9912", "To apply, email your CV to careers@example.com", "Any employer"),
    ("https://acme.myworkdayjobs.com/en-US/careers/job/R-9912", "", "Workday"),
    ("https://acme.taleo.net/careersection/jobdetail.ftl?job=1", "", "Taleo"),
    ("https://www.linkedin.com/jobs/view/4123456789", "", "LinkedIn"),
    ("https://www.indeed.com/viewjob?jk=abc", "", "Indeed"),
    ("https://in.naukri.com/job-listings-network-engineer-1", "", "Naukri"),
    ("https://www.dice.com/job-detail/abc", "", "Dice"),
    ("https://www.glassdoor.com/job-listing/x", "", "Glassdoor"),
]
channels = []
for url, description, label in CHANNEL_CASES:
    channel = detect(url=url, description=description)
    channels.append({**channel.to_dict(), "board": label})

# --- 3. an autopilot pass ---------------------------------------------------
from careeros.apply.guardrails import AutopilotPolicy
from careeros.db.models import EvidenceItem, Job
from careeros.enums import VerificationState
from careeros.pipeline import Pipeline
from careeros.sources.jsonfile import JsonFileSource

Pipeline(session).ingest([JsonFileSource("data/sample_jobs.json")])
session.commit()

cobalt = session.query(Job).filter(Job.company == "Cobalt Bank").first()
cobalt.url = "https://boards.greenhouse.io/cobaltbank/jobs/5550001"
halden = session.query(Job).filter(Job.company == "Halden Consulting").first()
halden.description = (halden.description or "") + (
    "\n\nTo apply, email your CV to careers@halden.example.com"
)
for item in session.query(EvidenceItem).all():
    item.verification = VerificationState.VERIFIED.value
session.commit()

from careeros.autopilot import run_once, save_policy

policy = AutopilotPolicy(enabled=True, dry_run=True, min_match_score=50.0, interval_hours=4.0)
save_policy(session, user, policy)
session.commit()

report = run_once(session, user, discover_jobs=False, use_ai=False)
session.commit()

# --- write it into the fixtures --------------------------------------------
path = Path("clients/app/src/demoFixtures.json")
data = json.loads(path.read_text(encoding="utf-8"))
data["resumeIntake"] = intake
data["channels"] = channels
data["autopilot"] = {
    "policy": policy.to_dict(),
    "summary": policy.summary(),
    "never_configurable": [
        "The tailored résumé must pass the factuality check.",
        "It must cite at least one verified evidence item.",
        "A work-authorization verdict of not_compatible stops the application.",
        "No submission bypasses a CAPTCHA, a login wall or bot protection.",
    ],
}
data["autopilotRun"] = json.loads(json.dumps(report.to_dict(), default=str))
path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

print("resume:", intake["parsed"]["counts"], "terms:", len(intake["search_terms"]))
print("channels:", len(channels))
counts = report.to_dict()["counts"]
print("autopilot pass:", counts)
print("blocked_by keys:", len(report.blocked_counts))
print("fixture groups now:", len(data))

# --- 4. ATS alignment on one posting ---------------------------------------
from careeros.db.models import AtsAssessment
from careeros.engines.ats_align import AtsAligner
from careeros.engines.resume import Resume, ResumeBlock, ResumeEntry, ResumeSection
from careeros.enums import ResumeKind
from careeros.services import load_evidence_records

target = session.query(Job).filter(Job.company == "Cobalt Bank").first()
rows = (
    session.query(AtsAssessment)
    .filter(AtsAssessment.job_id == target.id)
    .order_by(AtsAssessment.id)
    .all()
)
doc = (
    session.query(__import__("careeros.db.models", fromlist=["ResumeDocument"]).ResumeDocument)
    .filter_by(job_id=target.id, kind=ResumeKind.TAILORED.value)
    .order_by(__import__("careeros.db.models", fromlist=["ResumeDocument"]).ResumeDocument.version.desc())
    .first()
)
tailored_row = next((r for r in rows if doc and r.resume_id == doc.id), None)
baseline_row = next((r for r in rows if r is not tailored_row), None)

rebuilt = Resume(name=doc.name, kind=ResumeKind.TAILORED)
for sec in doc.sections or []:
    rebuilt.sections.append(
        ResumeSection(
            name=sec.get("name", ""),
            kind=sec.get("kind", ""),
            entries=[
                ResumeEntry(
                    heading=en.get("heading"),
                    subheading=en.get("subheading"),
                    meta=en.get("meta"),
                    blocks=[
                        ResumeBlock(text=b.get("text", ""), evidence_ids=list(b.get("evidence_ids") or []))
                        for b in en.get("blocks") or []
                    ],
                )
                for en in sec.get("entries") or []
            ],
        )
    )
plan = AtsAligner().align(
    rebuilt, load_evidence_records(session, user.id), target.classification.ats_keywords, apply=False
)

data = json.loads(path.read_text(encoding="utf-8"))
data["atsReport"] = {
    "job": f"{target.title} — {target.company}",
    "keyword_count": len(target.classification.ats_keywords or []),
    "keywords_from_posting": list(target.classification.ats_keywords or [])[:24],
    "baseline": {"overall": baseline_row.overall, "keyword_match": baseline_row.keyword_match}
    if baseline_row
    else None,
    "tailored": {"overall": tailored_row.overall, "keyword_match": tailored_row.keyword_match}
    if tailored_row
    else None,
    "lift": {
        "overall": round(tailored_row.overall - baseline_row.overall, 1),
        "keyword_match": round(tailored_row.keyword_match - baseline_row.keyword_match, 1),
    }
    if (baseline_row and tailored_row)
    else None,
    "alignment": plan.to_dict(),
}
path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
print("ats report captured:", data["atsReport"]["baseline"], "->", data["atsReport"]["tailored"])
print("  coverage:", plan.coverage, "added:", plan.added)
