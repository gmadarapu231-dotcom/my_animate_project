"""Capture the autopilot's real output into the demo fixtures.

Runs on its own (unlike capture_fixtures.py it needs no résumé file) and only
rewrites the three autopilot groups: the policy, one live pass held for the
user's approval, and the queue that pass left behind.

    python3 clients/demo-page/capture_autopilot.py
"""
import json
import os
import tempfile
from pathlib import Path

os.environ.update(
    CAREEROS_HOME=tempfile.mkdtemp(),
    CAREEROS_DATABASE_URL=f"sqlite:///{tempfile.mkdtemp()}/cap.db",
    CAREEROS_LLM="off",
    CAREEROS_GMAIL_TOKEN=os.path.join(tempfile.mkdtemp(), "none.json"),
)
os.environ.pop("CAREEROS_LOCAL_MAILBOX", None)

from careeros.api.routers.autopilot import _describe
from careeros.apply.guardrails import AutopilotPolicy
from careeros.autopilot import approval_queue, run_once, save_policy
from careeros.db.models import EvidenceItem, Job
from careeros.db.session import init_db, new_session
from careeros.enums import VerificationState
from careeros.pipeline import Pipeline
from careeros.profile_io import load_profile
from careeros.sources.jsonfile import JsonFileSource

init_db()
session = new_session()
user = load_profile(session, "data/sample_profile.yaml")
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

# Live, but every application waits for a yes: nothing reaches a network.
policy = AutopilotPolicy(
    enabled=True, dry_run=False, require_approval=True, min_match_score=50.0, interval_hours=4.0
)
save_policy(session, user, policy)
session.commit()

report = run_once(session, user, discover_jobs=False, use_ai=False)
session.commit()

path = Path("clients/app/src/demoFixtures.json")
data = json.loads(path.read_text(encoding="utf-8"))
data["autopilot"] = _describe(policy)
data["autopilotRun"] = json.loads(json.dumps(report.to_dict(), default=str))
data["autopilotQueue"] = {"count": 0, "items": approval_queue(session, user)}
data["autopilotQueue"]["count"] = len(data["autopilotQueue"]["items"])
path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

print("pass:", report.to_dict()["counts"])
print("queue:", [(q["company"], q["tier"]) for q in data["autopilotQueue"]["items"]])
