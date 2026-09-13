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
