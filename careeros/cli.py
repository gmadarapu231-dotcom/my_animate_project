"""CareerOS command line.

    careeros init                       create the database and seed the taxonomy
    careeros load-profile FILE          import the Master Profile + evidence
    careeros ingest FILE                fetch and classify postings
    careeros run-daily [FILE]           the full morning run
    careeros jobs                       ranked job list
    careeros show JOB_ID                full analysis for one job
    careeros tailor JOB_ID              tailor a resume + factuality check
    careeros search "QUERY"             natural-language search
    careeros recommend                  what should I apply for today?
    careeros analytics                  funnel + career learning signals
    careeros email-sync MAILBOX.json    classify job mail, draft replies
    careeros gmail-auth                 OAuth consent flow (never a password)
    careeros agent "QUESTION"           ask the agentic loop (needs model access)
    careeros agent-runs                 audit trail of past agent runs
    careeros serve                      dashboard + API
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import textwrap
from pathlib import Path
from careeros.ai.provider import get_provider
from careeros.db.session import database_url, init_db, session_scope
from careeros.enums import DeadlineBucket

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"


def _p(text: str = "") -> None:
    print(text)


def _require_user(session):
    from careeros.services import load_user

    user = load_user(session)
    if user is None:
        sys.exit("No profile loaded. Run: careeros load-profile data/sample_profile.yaml")
    return user


def _pipeline(session):
    from careeros.pipeline import Pipeline

    return Pipeline(session)


# ---------------------------------------------------------------------------
def cmd_init(args: argparse.Namespace) -> None:
    init_db()
    from careeros.config import countries, taxonomy

    _p(f"{BOLD}CareerOS initialised{RESET}")
    _p(f"  database   {database_url()}")
    _p(f"  countries  {', '.join(p.code + ' (' + p.name + ')' for p in countries().all())}")
    _p(f"  taxonomy   {len(taxonomy().domains)} seed domains, {len(taxonomy().skills)} skill nodes")
    provider = get_provider()
    _p(f"  AI layer   {provider.name} ({'available' if provider.available else 'heuristics only'})")


def cmd_load_profile(args: argparse.Namespace) -> None:
    from careeros.profile_io import load_profile
    from careeros.services import load_evidence_records

    with session_scope() as session:
        user = load_profile(session, args.path)
        session.flush()
        count = len(load_evidence_records(session, user.id))
        _p(f"Loaded {BOLD}{user.full_name}{RESET} <{user.email}>")
        _p(f"  {count} evidence items in the Career Evidence Database")


def cmd_ingest(args: argparse.Namespace) -> None:
    from careeros.sources.jsonfile import JsonFileSource

    with session_scope() as session:
        user = _require_user(session)
        pipe = _pipeline(session)
        stats = pipe.ingest([JsonFileSource(args.path)], limit=args.limit)
        pipe.classify_jobs(stats=stats)
        pipe.assess_for_user(user, stats=stats)
        _p(json.dumps(stats.to_dict(), indent=2))


def cmd_run_daily(args: argparse.Namespace) -> None:
    from careeros.sources.jsonfile import JsonFileSource

    with session_scope() as session:
        user = _require_user(session)
        pipe = _pipeline(session)
        sources = [JsonFileSource(args.path)] if args.path else []
        run = pipe.run_daily(
            sources, user=user, tailor_limit=args.tailor_limit, use_ai=not args.no_ai
        )
        session.flush()
        _p(f"{BOLD}Daily run {'OK' if run.ok else 'completed with errors'}{RESET}")
        _p(json.dumps(run.stats, indent=2))
        for err in run.errors:
            _p(f"  ! {err}")


def cmd_jobs(args: argparse.Namespace) -> None:
    with session_scope() as session:
        _require_user(session)      # exits with guidance when no profile is loaded
        pipe = _pipeline(session)
        jobs = pipe.ranked_jobs(limit=args.limit, include_archived=args.all)
        if args.country:
            jobs = [j for j in jobs if j.country_code == args.country.upper()]
        if args.domain:
            jobs = [j for j in jobs if j.classification and j.classification.domain_id == args.domain]

        _p(f"{BOLD}{'ID':>4} {'PRI':>6} {'MATCH':>6} {'ELIG':>5}  {'DEADLINE':<27} {'DOMAIN':<16} JOB{RESET}")
        for job in jobs:
            pr, cl = job.priority, job.classification
            bucket = DeadlineBucket(pr.deadline_bucket) if pr else DeadlineBucket.NONE
            _p(
                f"{job.id:>4} {pr.overall if pr else 0:>6.1f} {pr.match_score if pr else 0:>6.1f} "
                f"{pr.eligibility_score if pr else 0:>5.0f}  "
                f"{bucket.emoji + ' ' + bucket.label:<27} "
                f"{(cl.domain_id if cl else '?'):<16} {job.title} @ {job.company} [{job.country_code}]"
            )
        if not jobs:
            _p(f"{DIM}No jobs. Try: careeros run-daily data/sample_jobs.json{RESET}")


def cmd_show(args: argparse.Namespace) -> None:
    from careeros.api.serializers import job_card
    from careeros.db.models import Job

    with session_scope() as session:
        user = _require_user(session)
        job = session.get(Job, args.job_id)
        if job is None:
            sys.exit(f"No job with id {args.job_id}")
        card = job_card(session, job, user_id=user.id)
        _p(json.dumps(card, indent=2, default=str))


def cmd_tailor(args: argparse.Namespace) -> None:
    from careeros.db.models import Job

    with session_scope() as session:
        user = _require_user(session)
        job = session.get(Job, args.job_id)
        if job is None:
            sys.exit(f"No job with id {args.job_id}")
        pipe = _pipeline(session)
        doc, report = pipe.tailor_for_job(user, job, use_ai=not args.no_ai)
        session.flush()
        _p(f"{BOLD}{doc.name}{RESET}  ->  {doc.storage_path}")
        _p(f"Factuality: {'PASS' if report.passed else 'FAIL'} - {report.summary}")
        for note in doc.tailoring_notes:
            _p(f"  * {note}")
        for claim in report.unsupported:
            _p(f"  ! UNSUPPORTED: {claim.text[:90]}")
            for issue in claim.issues:
                _p(f"      - {issue}")
        if args.print:
            _p("\n" + "=" * 72 + "\n" + (doc.rendered_text or ""))


def cmd_search(args: argparse.Namespace) -> None:
    from careeros.search import QueryParser, run_search

    with session_scope() as session:
        user = _require_user(session)
        filters = QueryParser().parse(args.query, use_ai=not args.no_ai)
        jobs = run_search(session, filters, user_id=user.id, limit=args.limit)
        _p(f"{DIM}interpreted as: {filters.describe()}{RESET}")
        for job in jobs:
            pr = job.priority
            _p(
                f"  [{job.id:>3}] {pr.overall if pr else 0:>5.1f}  {job.title} @ {job.company} "
                f"[{job.country_code}]"
            )
        if not jobs:
            _p(f"{DIM}  no matches{RESET}")


def cmd_recommend(args: argparse.Namespace) -> None:
    with session_scope() as session:
        user = _require_user(session)
        rec = _pipeline(session).recommend(user)
        session.flush()
        _p(rec.narrative or "No recommendation available.")


def cmd_analytics(args: argparse.Namespace) -> None:
    from careeros.analytics import Analytics

    with session_scope() as session:
        user = _require_user(session)
        data = Analytics(session).dashboard(user.id)
        overall = data["overall"]
        _p(f"{BOLD}Funnel{RESET}")
        for key in ("applications", "responses", "interviews", "assessments", "offers", "rejections"):
            _p(f"  {key:<14} {overall[key]}")
        _p(f"  {'response rate':<14} {overall['response_rate']}%")
        _p(f"  {'interview rate':<14} {overall['interview_rate']}%")
        _p(f"  {'offer rate':<14} {overall['offer_rate']}%")
        if overall.get("note"):
            _p(f"  {DIM}{overall['note']}{RESET}")
        for row in data["by_track"]:
            _p(f"  track {row['label']:<24} {row['applications']} apps, {row['interviews']} interviews")
        _p(f"\n{BOLD}Learning signals{RESET}")
        for signal in data["learning_signals"]:
            _p(f"  [{signal['kind']}] {signal['subject']} ({signal['confidence']})")
            _p(f"      {signal['rationale']}")
        if not data["learning_signals"]:
            _p(f"  {DIM}Not enough outcome history yet.{RESET}")


def cmd_email_sync(args: argparse.Namespace) -> None:
    from careeros.gmail.client import get_client
    from careeros.gmail.sync import GmailSync

    with session_scope() as session:
        user = _require_user(session)
        client = get_client(args.mailbox)
        stats = GmailSync(session, client).sync(user, limit=args.limit, use_ai=not args.no_ai)
        _p(json.dumps(stats.to_dict(), indent=2))


def cmd_gmail_auth(args: argparse.Namespace) -> None:
    """Run the OAuth consent flow. CareerOS never accepts a Gmail password."""
    client_secrets = args.client_secrets or os.getenv("CAREEROS_GMAIL_CLIENT_SECRETS")
    if not client_secrets:
        sys.exit(
            "Pass --client-secrets /path/to/client_secret.json (download it from your "
            "Google Cloud project: APIs & Services -> Credentials -> OAuth client ID, "
            "type 'Desktop app')."
        )
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: PLC0415
    except ImportError:
        sys.exit("Install the gmail extra:  pip install 'careeros[gmail]'")

    from careeros.gmail.client import DEFAULT_SCOPES, SEND_SCOPE

    scopes = list(DEFAULT_SCOPES) + ([SEND_SCOPE] if args.allow_send else [])
    flow = InstalledAppFlow.from_client_secrets_file(client_secrets, scopes)
    creds = flow.run_local_server(port=0)
    token_path = Path(
        args.token or os.getenv("CAREEROS_GMAIL_TOKEN", Path.home() / ".careeros" / "gmail_token.json")
    )
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    token_path.chmod(0o600)
    _p(f"Token written to {token_path} (mode 600). Scopes: {', '.join(scopes)}")


def cmd_agent(args: argparse.Namespace) -> None:
    """Run the agentic loop: Claude drives the engines as tools."""
    from careeros.agent import AgentUnavailable, CareerAgent

    with session_scope() as session:
        user = _require_user(session)
        agent = CareerAgent(
            session,
            user,
            max_turns=args.max_turns,
            effort=args.effort,
            use_ai_inside_tools=not args.no_ai_in_tools,
        )
        try:
            result = agent.run(args.prompt)
        except AgentUnavailable as exc:
            sys.exit(str(exc))
        session.flush()

        if args.json:
            _p(json.dumps(result.to_dict(), indent=2, default=str))
            return

        if args.show_tools:
            _p(f"{DIM}{len(agent.tools)} tools available{RESET}")
        for call in result.tool_calls:
            flag = "!" if call.is_error else ("*" if call.mutating else " ")
            args_text = ", ".join(f"{k}={v}" for k, v in call.arguments.items())
            _p(f"{DIM}  {flag} turn {call.turn}  {call.name}({args_text}){RESET}")

        _p("")
        _p(result.answer)
        _p("")
        summary = (
            f"{result.turns} turn(s), {len(result.tool_calls)} tool call(s), "
            f"{result.input_tokens + result.output_tokens} tokens"
        )
        if result.mutations:
            summary += f", changed: {', '.join(sorted(set(result.mutations)))}"
        _p(f"{DIM}{summary}{RESET}")
        if not result.ok:
            _p(f"  ! {result.error}")


def cmd_agent_runs(args: argparse.Namespace) -> None:
    from sqlalchemy import select

    from careeros.db.models import AgentRun, AgentToolCall

    with session_scope() as session:
        _require_user(session)
        runs = session.scalars(
            select(AgentRun).order_by(AgentRun.id.desc()).limit(args.limit)
        ).all()
        if not runs:
            _p(f"{DIM}No agent runs recorded yet.{RESET}")
            return
        for run in runs:
            status = "ok" if run.ok else "FAILED"
            _p(f"{BOLD}#{run.id}{RESET} [{status}] {run.started_at:%Y-%m-%d %H:%M} "
               f"- {run.turns} turn(s), {run.input_tokens + run.output_tokens} tokens")
            _p(f'  prompt: "{run.prompt[:100]}"')
            calls = session.scalars(
                select(AgentToolCall).where(AgentToolCall.run_id == run.id)
                .order_by(AgentToolCall.id)
            ).all()
            for call in calls:
                flag = "!" if call.is_error else ("*" if call.mutating else " ")
                _p(f"{DIM}    {flag} {call.name}{RESET}")
            if run.error:
                _p(f"  ! {run.error}")


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn  # noqa: PLC0415

    from careeros.api.app import EXPO_WEB_DIR
    from careeros.api.security import auth_required

    base = f"http://{args.host}:{args.port}"
    if EXPO_WEB_DIR.exists():
        _p(f"Web app    {base}/            (universal app, same code as iOS/Android)")
    else:
        _p(f"Dashboard  {base}/            (built-in; no build step)")
        _p(f"{DIM}           build the universal web app with:"
           f" npm --prefix clients/app run export:web{RESET}")
    _p(f"Classic    {base}/classic")
    _p(f"API docs   {base}/docs")

    if args.host not in {"127.0.0.1", "localhost"} and not auth_required():
        # Binding to a network interface without a token exposes the whole
        # career history to anyone on that network.
        _p("")
        _p(f"{BOLD}WARNING{RESET}: serving on {args.host} with no API token.")
        _p("  Anyone on this network can read and change your data. Set one first:")
        _p("    export CAREEROS_API_TOKEN=$(openssl rand -hex 24)")
        _p("  then enter the same value in the app's Settings screen.")
    _p("")
    uvicorn.run("careeros.api.app:app", host=args.host, port=args.port, reload=args.reload)


def cmd_export(args: argparse.Namespace) -> None:
    import yaml  # noqa: PLC0415

    from careeros.profile_io import export_profile

    with session_scope() as session:
        user = _require_user(session)
        data = export_profile(session, user)
        text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            _p(f"Wrote {args.out}")
        else:
            _p(text)


# ---------------------------------------------------------------------------
def cmd_sources(args: argparse.Namespace) -> None:
    """Where jobs can come from, and what each provider still needs."""
    from careeros.sources import provider_status

    status = provider_status(country=args.country)
    if args.json:
        print(json.dumps(status, indent=2))
        return

    scope = f" for {args.country}" if args.country else ""
    print(f"{status['count']} providers{scope}: {status['ready']} ready, "
          f"{status['needs_credentials']} need credentials, {status['not_permitted']} not fetched\n")

    groups = {"ready": [], "needs_credentials": [], "not_permitted": []}
    for row in status["providers"]:
        groups[row["state"]].append(row)

    if groups["ready"]:
        print("READY")
        for row in groups["ready"]:
            note = "" if row["endpoint_verified"] else "  (endpoint unverified)"
            print(f"  {row['id']:<24} {row['access_label']:<22} {', '.join(row['countries'])}{note}")
    if groups["needs_credentials"]:
        print("\nNEEDS CREDENTIALS")
        for row in groups["needs_credentials"]:
            print(f"  {row['id']:<24} set {', '.join(row['missing_env'])}")
    if groups["not_permitted"]:
        print("\nNOT FETCHED")
        for row in groups["not_permitted"]:
            reason = " ".join((row["reason"] or "").split()).split(". ")[0].rstrip(".")
            print(f"  {row['id']:<24} {textwrap.shorten(reason, width=88, placeholder=' ...')}")
            if row["use_instead"]:
                print(f"  {'':<24} -> use {', '.join(row['use_instead'])}")
    if args.verbose:
        print()
        for row in status["providers"]:
            if row["notes"]:
                print(f"{row['id']}:\n  " + row["notes"].replace("\n", "\n  ") + "\n")


def cmd_discover(args: argparse.Namespace) -> None:
    """Search every ready provider for this profile and ingest the results."""
    from careeros.sources import build_registry
    from careeros.sources.base import SourceRegistry
    from careeros.sources.discover import discover

    with session_scope() as session:
        user = _require_user(session)
        report = discover(
            session,
            user,
            registry=build_registry(into=SourceRegistry()),
            terms=args.term or None,
            countries=[c.upper() for c in (args.country or [])] or None,
            only=args.only or None,
            per_provider=args.per_provider,
            ingest=not args.dry_run,
        )

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return

    plan = report.get("plan") or {}
    print(f"Searched {len(plan.get('searches', []))} query/provider pairs "
          f"across {', '.join(plan.get('countries', [])) or '-'}")
    print(f"Terms: {', '.join(plan.get('terms', [])) or '(none - add career tracks to your profile)'}\n")

    for row in report["providers"]:
        if row["skipped_reason"]:
            print(f"  -- {row['provider']:<24} skipped: {row['skipped_reason']}")
        else:
            errors = f"  ({len(row['errors'])} error(s))" if row["errors"] else ""
            print(f"  ok {row['provider']:<24} {row['found']:>4} posting(s) from {row['searches']} search(es){errors}")
        for error in row["errors"][:2]:
            print(f"     ! {error}")

    if report["by_board"]:
        print("\nBy board:")
        for board, count in report["by_board"].items():
            print(f"  {count:>4}  {board}")

    ingested = report.get("ingest")
    if ingested:
        print(f"\nIngested: {ingested['inserted']} new, {ingested['duplicates']} already known "
              f"(of {ingested['fetched']} fetched)")
        print("Run `careeros run-daily` to classify and rank them, then `careeros jobs`.")
    elif args.dry_run:
        print(f"\nDry run: {report['found']} posting(s) found, nothing saved.")


def cmd_signin(args: argparse.Namespace) -> None:
    """Issue a session token for an email address, for use by a client."""
    from careeros.auth import describe_auth
    from careeros.auth.service import Identity, upsert_account
    from careeros.auth.tokens import issue_token

    if args.describe:
        print(json.dumps(describe_auth(), indent=2))
        return

    with session_scope() as session:
        user, created = upsert_account(
            session, Identity(email=args.email, name=args.name, method="cli")
        )
        token = issue_token(user.id, user.email, method="cli")
        user_id, email = user.id, user.email

    print(f"{'Created' if created else 'Found'} account {email} (id {user_id})")
    print("\nSession token (treat it like a password):\n")
    print(token)
    print(
        "\nPaste it into the app's sign-in screen, or send it as"
        "\n  Authorization: Bearer <token>"
    )


def cmd_resume(args: argparse.Namespace) -> None:
    """Read a résumé: fill the profile, propose evidence, derive search terms."""
    from careeros.resume_intake import ExtractionError, intake_file

    with session_scope() as session:
        user = _require_user(session)
        try:
            report = intake_file(
                session,
                user,
                args.path,
                commit=not args.dry_run,
                overwrite_profile=args.overwrite,
            )
        except ExtractionError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(2)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return

    parsed = report["parsed"]
    print(f"{report['file']['name']}  {report['file']['characters']} characters, "
          f"sections: {', '.join(parsed['sections_found']) or 'none recognised'}\n")

    print(f"  {parsed['full_name'] or '(no name found)'}")
    if parsed["current_title"]:
        print(f"  {parsed['current_title']}")
    contact = " · ".join(filter(None, [parsed["email"], parsed["phone"]]))
    if contact:
        print(f"  {contact}")
    print(f"  {parsed['total_experience_years']} years across {len(parsed['employers'])} role(s)\n")

    for employer in parsed["employers"]:
        span = f"{employer['start_date'] or '?'} -> {'present' if employer['current'] else employer['end_date'] or '?'}"
        where = f" · {employer['location']}" if employer["location"] else ""
        print(f"  {employer['name']}")
        print(f"    {employer['title'] or '(no title)'}{where} · {span}")
    print()

    print(f"  {len(parsed['evidence'])} evidence proposal(s):")
    for item in parsed["evidence"][:8]:
        marker = "!" if item["kind"] == "achievement" else "·"
        metrics = f"  {item['metrics']}" if item["metrics"] else ""
        print(f"    {marker} {textwrap.shorten(item['text'], width=76, placeholder=' ...')}{metrics}")
    if len(parsed["evidence"]) > 8:
        print(f"    ... and {len(parsed['evidence']) - 8} more")

    if parsed["skills"]:
        top = sorted(parsed["skills"].items(), key=lambda kv: -kv[1])[:12]
        print(f"\n  Skills matched ({len(parsed['skills'])}): " + ", ".join(k for k, _ in top))
    if parsed["certifications"]:
        print(f"  Certifications: {', '.join(parsed['certifications'][:6])}")

    print(f"\n  Search terms derived: {', '.join(report['search_terms'])}")

    for warning in parsed["warnings"]:
        print(f"\n  ! {warning}")

    if report["committed"]:
        intake = report["intake"]
        print(f"\nSaved: {intake['employers_created']} new employer(s), "
              f"{intake['evidence_created']} evidence item(s), "
              f"{intake['certifications_created']} certification(s)")
        if intake["evidence_skipped"]:
            print(f"       {intake['evidence_skipped']} already known, skipped")
        print(f"\n{intake['next_step']}")
        print("Nothing on a generated résumé can cite an unverified item.")
        print("  careeros verify-evidence --all        confirm everything read from this file")
        print("  careeros discover                    search using these terms")
    else:
        print("\nDry run: nothing saved.")


def cmd_verify_evidence(args: argparse.Namespace) -> None:
    """Confirm evidence the parser proposed. This is the human gate."""
    from careeros.db.models import EvidenceItem
    from careeros.enums import VerificationState
    from careeros.resume_intake import verify

    with session_scope() as session:
        user = _require_user(session)
        rows = (
            session.query(EvidenceItem)
            .filter(EvidenceItem.user_id == user.id)
            .order_by(EvidenceItem.id)
            .all()
        )
        unverified = [r for r in rows if r.verification == VerificationState.UNVERIFIED.value]

        if args.list or not (args.all or args.id):
            if not unverified:
                print("Nothing unverified. Every evidence item is confirmed.")
                return
            print(f"{len(unverified)} unverified item(s):\n")
            for row in unverified:
                print(f"  [{row.id:>3}] {row.kind:<14} {textwrap.shorten(row.text, width=84, placeholder=' ...')}")
            print("\nConfirm the accurate ones:")
            print("  careeros verify-evidence --id 3 --id 4")
            print("  careeros verify-evidence --all")
            return

        targets = [r.id for r in unverified] if args.all else [int(i) for i in args.id]
        changed = verify(session, user, targets, verified=not args.undo)

    verb = "unverified" if args.undo else "verified"
    print(f"{changed} item(s) {verb}.")


#: Policy fields settable by a `--flag value` option. `enabled` and `dry_run`
#: are deliberately NOT here: they are store_true flags, whose default is False
#: rather than None, so a generic "if the flag is not None, apply it" loop would
#: read a *missing* --dry-run as "go live". That is the one mistake this module
#: must not make, so the two switches are handled explicitly below.
_AUTOPILOT_FIELDS = (
    "interval_hours", "max_per_run", "max_per_day",
    "min_match_score", "min_priority_score", "channels", "allow_unknown_verdict",
    "require_deadline_open", "company_blocklist", "company_allowlist",
    "domains", "countries", "min_salary", "skip_if_applicants_over", "quiet_hours",
)

#: The two fields that display order shows first.
_AUTOPILOT_SWITCHES = ("enabled", "dry_run")


def _print_report(report) -> None:
    data = report.to_dict()
    if report.skipped_reason:
        print(f"Skipped: {report.skipped_reason}")
        return

    counts = data["counts"]
    print(f"Pass finished in {data['duration_seconds']}s\n")
    if data["discovery"]:
        found = data["discovery"]["found"]
        ingested = (data["discovery"].get("ingest") or {}).get("inserted")
        tail = f", {ingested} new" if ingested is not None else ""
        print(f"  Found      {found} posting(s) from providers{tail}")
    print(f"  Classified {counts['classified']}")
    print(f"  Tailored   {counts['tailored']} résumé(s)")
    print(f"  Considered {counts['considered']} job(s)")
    if counts["submitted"]:
        print(f"  SUBMITTED  {counts['submitted']}")
    if counts["would_submit"]:
        print(f"  Would send {counts['would_submit']} (dry run)")
    print(f"  For you    {counts['assisted']} need submitting by hand")

    acted = [v for v in report.verdicts if v.allowed]
    if acted:
        print("\nApplied:" if counts["submitted"] else "\nWould apply:")
        for verdict in acted:
            message = (verdict.submission or {}).get("message", "")
            print(f"  {verdict.company} — {verdict.title}")
            print(f"    [{verdict.tier}] {textwrap.shorten(message, width=92, placeholder=' ...')}")
            for note in verdict.notes:
                print(f"    note: {textwrap.shorten(note, width=88, placeholder=' ...')}")

    if data["blocked_by"]:
        print("\nNot applied, by reason:")
        for reason, count in data["blocked_by"].items():
            print(f"  {count:>3}  {textwrap.shorten(reason, width=90, placeholder=' ...')}")

    for error in data["errors"][:5]:
        print(f"\n  ! {error}")


def cmd_autopilot(args: argparse.Namespace) -> None:
    """Search and apply on a timer, within rules you set."""
    from careeros.apply.guardrails import AutopilotPolicy
    from careeros.autopilot import (
        AlreadyRunning,
        RunLock,
        load_policy,
        lock_path,
        run_forever,
        run_once,
        save_policy,
    )
    from careeros.db.session import new_session

    action = args.action or "status"

    if action in ("enable", "disable", "set"):
        with session_scope() as session:
            user = _require_user(session)
            policy = load_policy(user)
            if action == "enable":
                policy.enabled = True
            elif action == "disable":
                policy.enabled = False
            for field in _AUTOPILOT_FIELDS:
                value = getattr(args, field, None)
                if value is None:
                    continue
                current = getattr(policy, field)
                setattr(policy, field, tuple(value) if isinstance(current, tuple) else value)
            # Going live is only ever an explicit act.
            if args.live and args.dry_run:
                print("--live and --dry-run contradict each other.", file=sys.stderr)
                raise SystemExit(2)
            if args.live:
                policy.dry_run = False
            elif args.dry_run:
                policy.dry_run = True
            save_policy(session, user, policy)
            summary, as_dict = policy.summary(), policy.to_dict()

        print(summary)
        if args.json:
            print(json.dumps(as_dict, indent=2))
        elif as_dict["enabled"] and as_dict["dry_run"]:
            print("\nStill a dry run: it assembles and reports, and sends nothing.")
            print("When the reports look right:  careeros autopilot set --live")
        elif as_dict["enabled"]:
            print("\nLIVE. Applications will be submitted automatically.")
            print(f"Caps: {as_dict['max_per_run']} per run, {as_dict['max_per_day']} per day.")
        return

    if action == "status":
        with session_scope() as session:
            user = _require_user(session)
            policy = load_policy(user)
            as_dict = policy.to_dict()
            summary = policy.summary()
        if args.json:
            print(json.dumps(as_dict, indent=2))
            return
        print(summary + "\n")
        for key in _AUTOPILOT_SWITCHES + _AUTOPILOT_FIELDS:
            value = as_dict[key]
            shown = ", ".join(str(v) for v in value) if isinstance(value, list) else value
            print(f"  {key:<24} {shown if shown not in ([], '', None) else '-'}")
        lock = lock_path()
        print(f"\n  lock file                {lock} {'(held)' if lock.exists() else ''}")
        return

    if action == "install":
        interval = args.interval_hours or 4
        binary = shutil.which("careeros") or "careeros"
        print("Two ways to run this every "
              f"{interval:g} hours. A timer is sturdier than a loop: it survives a reboot.\n")
        print("cron — `crontab -e`, then add:")
        print(f"  0 */{int(interval)} * * *  {binary} autopilot once >> ~/.careeros/autopilot.log 2>&1\n")
        print("systemd — ~/.config/systemd/user/careeros.service:")
        print("  [Unit]\n  Description=CareerOS autopilot\n")
        print(f"  [Service]\n  Type=oneshot\n  ExecStart={binary} autopilot once\n")
        print("~/.config/systemd/user/careeros.timer:")
        print("  [Unit]\n  Description=Run CareerOS autopilot\n")
        print(f"  [Timer]\n  OnBootSec=10min\n  OnUnitActiveSec={int(interval)}h\n  Persistent=true\n")
        print("  [Install]\n  WantedBy=timers.target\n")
        print("  systemctl --user daemon-reload && systemctl --user enable --now careeros.timer\n")
        print(f"In the foreground instead:  {binary} autopilot loop")
        return

    if action == "once":
        try:
            with RunLock():
                with session_scope() as session:
                    report = run_once(
                        session,
                        discover_jobs=not args.no_discover,
                        use_ai=not args.no_ai,
                    )
        except AlreadyRunning as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1)
        if args.json:
            print(json.dumps(report.to_dict(), indent=2, default=str))
        else:
            _print_report(report)
        return

    if action == "loop":
        interval = args.interval_hours
        print(f"Running every {interval or 'policy interval'} hour(s). Ctrl-C to stop.\n")
        try:
            run_forever(
                new_session,
                interval_hours=interval,
                max_passes=args.passes,
                on_report=lambda r: (
                    print(f"\n=== {r.started_at:%Y-%m-%d %H:%M} UTC ==="),
                    _print_report(r),
                ),
            )
        except KeyboardInterrupt:
            print("\nStopped.")
        return

    print(f"Unknown action {action!r}.", file=sys.stderr)
    raise SystemExit(2)


def _boolish(value: str) -> bool:
    """argparse type for an explicit yes/no flag."""
    lowered = str(value).strip().lower()
    if lowered in ("1", "true", "yes", "y", "on"):
        return True
    if lowered in ("0", "false", "no", "n", "off"):
        return False
    raise argparse.ArgumentTypeError(f"expected yes or no, got {value!r}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="careeros",
        description="Universal AI career operating system - any profession, any supported country.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database and seed the taxonomy").set_defaults(func=cmd_init)

    p = sub.add_parser("load-profile", help="import a Master Profile YAML")
    p.add_argument("path")
    p.set_defaults(func=cmd_load_profile)

    p = sub.add_parser("ingest", help="fetch + classify postings from a JSON file")
    p.add_argument("path")
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("run-daily", help="full morning run")
    p.add_argument("path", nargs="?")
    p.add_argument("--tailor-limit", type=int, default=5)
    p.add_argument("--no-ai", action="store_true", help="deterministic engines only")
    p.set_defaults(func=cmd_run_daily)

    p = sub.add_parser("jobs", help="ranked job list")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--country")
    p.add_argument("--domain")
    p.add_argument("--all", action="store_true", help="include archived/expired")
    p.set_defaults(func=cmd_jobs)

    p = sub.add_parser("show", help="full analysis for one job")
    p.add_argument("job_id", type=int)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("tailor", help="tailor a resume and run the factuality check")
    p.add_argument("job_id", type=int)
    p.add_argument("--print", action="store_true")
    p.add_argument("--no-ai", action="store_true")
    p.set_defaults(func=cmd_tailor)

    p = sub.add_parser("search", help="natural-language search")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--no-ai", action="store_true")
    p.set_defaults(func=cmd_search)

    sub.add_parser("recommend", help="what should I apply for today?").set_defaults(func=cmd_recommend)
    sub.add_parser("analytics", help="funnel + learning signals").set_defaults(func=cmd_analytics)

    p = sub.add_parser("email-sync", help="classify job mail and draft replies")
    p.add_argument("mailbox", nargs="?", help="local JSON mailbox (omit to use Gmail)")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--no-ai", action="store_true")
    p.set_defaults(func=cmd_email_sync)

    p = sub.add_parser("gmail-auth", help="run the Gmail OAuth consent flow")
    p.add_argument("--client-secrets")
    p.add_argument("--token")
    p.add_argument("--allow-send", action="store_true", help="also request the send scope")
    p.set_defaults(func=cmd_gmail_auth)

    p = sub.add_parser("export-profile", help="dump the profile back to YAML")
    p.add_argument("--out")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("agent", help="ask the agentic loop (requires model access)")
    p.add_argument("prompt")
    p.add_argument("--max-turns", type=int, default=12)
    p.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--json", action="store_true", help="full transcript as JSON")
    p.add_argument("--show-tools", action="store_true")
    p.add_argument(
        "--no-ai-in-tools",
        action="store_true",
        help="tools use their deterministic paths (cheaper; the loop still needs the model)",
    )
    p.set_defaults(func=cmd_agent)

    p = sub.add_parser("agent-runs", help="audit trail of past agent runs")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_agent_runs)

    p = sub.add_parser("resume", help="read a résumé: fill the profile and propose evidence")
    p.add_argument("path", help="a .txt, .md, .docx or .pdf résumé")
    p.add_argument("--overwrite", action="store_true", help="replace profile fields already set")
    p.add_argument("--dry-run", action="store_true", help="show what was read, save nothing")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("verify-evidence", help="confirm evidence read from a résumé")
    p.add_argument("--id", action="append", help="evidence id to confirm (repeatable)")
    p.add_argument("--all", action="store_true", help="confirm every unverified item")
    p.add_argument("--list", action="store_true", help="list what is unverified")
    p.add_argument("--undo", action="store_true", help="return items to unverified")
    p.set_defaults(func=cmd_verify_evidence)

    p = sub.add_parser("sources", help="where jobs can come from, and what each needs")
    p.add_argument("--country", help="only providers covering this country, e.g. US or IN")
    p.add_argument("--verbose", action="store_true", help="include each provider's notes")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser("discover", help="search every ready provider and ingest the results")
    p.add_argument("--term", action="append", help="search term (repeatable; default: your career tracks)")
    p.add_argument("--country", action="append", help="country code (repeatable; default: where you can work)")
    p.add_argument("--only", action="append", help="restrict to a provider id (repeatable)")
    p.add_argument("--per-provider", type=int, default=60, help="cap postings per provider")
    p.add_argument("--dry-run", action="store_true", help="fetch and report, save nothing")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("signin", help="issue a session token for an email address")
    p.add_argument("email", nargs="?", default="", help="the account's email address")
    p.add_argument("--name", help="full name, used only when creating the account")
    p.add_argument("--describe", action="store_true", help="show which sign-in methods are configured")
    p.set_defaults(func=cmd_signin)

    p = sub.add_parser("autopilot", help="search and apply on a timer, within rules you set")
    p.add_argument(
        "action",
        nargs="?",
        choices=["status", "enable", "disable", "set", "once", "loop", "install"],
        help="status (default), enable, disable, set, once, loop, install",
    )
    p.add_argument("--live", action="store_true", help="stop dry-running and actually submit")
    p.add_argument("--dry-run", action="store_true", help="assemble and report, send nothing")
    p.add_argument("--interval-hours", type=float, help="how often to run (default 4)")
    p.add_argument("--max-per-run", type=int)
    p.add_argument("--max-per-day", type=int)
    p.add_argument("--min-match-score", type=float)
    p.add_argument("--min-priority-score", type=float)
    p.add_argument("--channels", action="append", choices=["api", "email"],
                   help="which submission channels may be automated (repeatable)")
    p.add_argument("--company-blocklist", action="append", help="never apply here (repeatable)")
    p.add_argument("--company-allowlist", action="append", help="only apply here (repeatable)")
    p.add_argument("--domains", action="append", help="restrict to these domain ids (repeatable)")
    p.add_argument("--countries", action="append", help="restrict to these countries (repeatable)")
    p.add_argument("--min-salary", type=float)
    p.add_argument("--skip-if-applicants-over", type=int)
    p.add_argument("--quiet-hours", action="append", help='e.g. "22:00-07:00" (repeatable)')
    p.add_argument("--allow-unknown-verdict", type=_boolish,
                   help="apply when sponsorship is not mentioned (default yes)")
    p.add_argument("--require-deadline-open", type=_boolish)
    p.add_argument("--no-discover", action="store_true", help="once/loop: skip the search step")
    p.add_argument("--no-ai", action="store_true", help="once/loop: heuristics only")
    p.add_argument("--passes", type=int, help="loop: stop after this many passes")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_autopilot)

    p = sub.add_parser("serve", help="run the dashboard + API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve)

    return parser


#: Commands that never open the database.
_NO_DB_COMMANDS = frozenset({"gmail-auth"})


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command not in _NO_DB_COMMANDS:
        # Idempotent, and it brings a database created by an older version up to
        # the current schema rather than failing on a missing table.
        init_db()
    try:
        args.func(args)
    except BrokenPipeError:
        # `careeros jobs | head` closes the pipe early. Redirect stdout to
        # devnull so the interpreter's shutdown flush does not re-raise.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
