# CareerOS

A universal, domain-agnostic AI career operating system: job discovery,
classification, work-authorization eligibility, evidence-based resumes,
applications, email and career analytics — for **any profession**, in any
supported country.

It is not a resume agent. It is organised around one hierarchy:

```
Country → Work authorization → Career track → Job → Eligibility
        → Priority → Resume → Application → Email → Interview → Outcome
```

## Two ways to run

**The pipeline** (`careeros run-daily`) is the scheduled, deterministic path:
fixed stages, no API key needed, every job processed the same way every morning.

**The agent** (`careeros agent "…"`) is the interactive one: Claude drives the
same engines as tools and decides what to investigate.

```bash
$ careeros agent "Why am I not getting interviews for cloud roles?"
  · turn 1  get_analytics()
  · turn 1  get_profile()
  · turn 2  search_jobs(query=cloud jobs)
  · turn 2  get_match_detail(job_id=2)
  · turn 3  get_ats_report(job_id=2)

  [answer]

  3 turns, 5 tool calls, 14,208 tokens
```

They share every engine — if the agent and the pipeline disagreed about a match
score, that would be a bug. The agent has autonomy over **strategy** and none
over **truthfulness**: there is no tool to add evidence, override a visa
verdict, mark a resume final, or send mail. Those capabilities do not exist, so
no prompt reaches them — and `GET /api/agent/tools` publishes the withheld list
with the reason for each. See [`docs/11-agent-architecture.md`](docs/11-agent-architecture.md).

## What "domain-agnostic" means here

A Network Engineer job today, an SAP Security job tomorrow, a Data Scientist, a
healthcare analyst, a financial analyst or a veterinary practice manager next
week — **with no code change**. Three mechanisms make that true rather than
aspirational:

1. **The taxonomy is a seed, not a schema.** 15 domains ship in YAML. A job that
   scores below the confidence floor is filed as `other`, never force-fitted.
2. **New domains are persisted.** The AI layer is instructed to *invent* a
   domain id when nothing fits; it becomes a row in `career_domain` and a
   first-class filter from that moment.
3. **ATS keywords are derived per posting.** No industry word list exists
   anywhere. A veterinary posting yields `veterinary`,
   `inventory of pharmaceuticals`; a SAP posting yields `sap grc access control`,
   `pfcg`. Same code.

Countries work the same way: `config/countries/*.yaml` drives work
authorization, salary formats, employment types and terminology. Adding Canada
is a file, not a commit — there is a test that proves it.

## Two invariants

**Never assume sponsorship.** A posting that says nothing yields `UNKNOWN`.
Every verdict shows the JD phrase behind it and carries
*"AI assessment — verify with employer/recruiter."*

**Never claim experience the user does not have.** Resumes are *assembled* from
atomic verified evidence; every block cites its evidence ids; a deterministic
factuality checker re-derives the entities in the final text and blocks any
resume whose claims do not trace back.

```
Tailoring a networking background to a Senior SAP Security Consultant posting:

  Gaps left unclaimed (required by the posting, unsupported by evidence): firefighter
  Adjacent experience NOT presented as direct: sap grc access control,
      composite roles, segregation of duties
  Factuality: PASS — 20 claims checked, 20 supported, 0 unsupported

  mentions "sap"?  False    mentions "pfcg"?  False    mentions "grc"?  False
```

## Quick start

```bash
pip install -e .

careeros init
careeros load-profile data/sample_profile.yaml
careeros run-daily data/sample_jobs.json
careeros jobs
careeros serve                       # dashboard at http://127.0.0.1:8000
```

No API key required for any of that. Everything above runs on deterministic
engines. Set `ANTHROPIC_API_KEY` (or run `ant auth login`) and the same commands
get sharper — better classification, invented domains for unfamiliar
professions, reworded resume bullets, natural email replies — and
`careeros agent` becomes available, which is the one path that genuinely needs
the model.

```
  ID    PRI  MATCH  ELIG  DEADLINE                    DOMAIN         JOB
   1   85.6   88.8    75  🔴 DEADLINE TODAY           networking     Senior Network Security Engineer @ Cobalt Bank [US]
   3   64.9   56.8    60  🟡 DEADLINE WITHIN 7 DAYS   sap            Senior SAP Security Consultant @ Halden Consulting [US]
   4   84.2   97.1   100  🟢 FUTURE                   networking     Network Engineer - Data Centre @ Sahyadri Technologies [IN]
   6   66.7   53.3   100  🟢 FUTURE                   ai_ml          Senior Data Scientist @ Kavach Analytics [IN]
   8   54.8   62.4    50  🟢 FUTURE                   healthcare     Healthcare Data Analyst @ Willow Health Partners [US]
   9   46.8   20.0    50  🟢 FUTURE                   other          Veterinary Practice Manager @ Brightpaw [US]
   2   15.5   88.1     0  🟠 DEADLINE WITHIN 48 HOURS cybersecurity  Cloud Security Engineer @ Vertex Health [US]
```

Note the ordering: a 64.9 closing in 7 days sits above an 84.2 closing later,
because deadline tier beats score. A strong 88.1 match sinks to the bottom
because the posting says it cannot sponsor. And the veterinary role classified
into `other` with keywords drawn from its own text.

## Commands

```
careeros init                      create the database, seed the taxonomy
careeros load-profile FILE         import the Master Profile + evidence
careeros ingest FILE               fetch and classify postings
careeros run-daily [FILE]          the full morning run
careeros jobs [--country --domain] ranked job list
careeros show JOB_ID               full analysis for one job
careeros tailor JOB_ID [--print]   tailor a resume + factuality check
careeros search "QUERY"            natural-language search
careeros recommend                 what should I apply for today?
careeros analytics                 funnel + career learning signals
careeros agent "QUESTION"          the agentic loop (needs model access)
careeros agent-runs                audit trail of past agent runs
careeros email-sync [MAILBOX]      classify job mail, draft replies
careeros gmail-auth                OAuth consent flow (never a password)
careeros export-profile            dump the profile back to YAML
careeros serve                     dashboard + API
```

Natural-language search understands the phrasings you would actually type:

```bash
careeros search "Find H1B-friendly cybersecurity jobs in Texas"
#   → domain cybersecurity, country US, region TX, sponsorship available
careeros search "Find SAP Security jobs in India"
careeros search "jobs with deadline today"
careeros search "jobs where my match is above 85%"
```

## What's implemented

| Capability | State |
|---|---|
| Country packs (USA + India), extensible by file | ✅ |
| Career taxonomy, extensible at runtime | ✅ |
| Skill graph (95 nodes) + cross-domain transferable matching | ✅ |
| Career Evidence Database | ✅ |
| Job classification, any profession | ✅ |
| Work-authorization engine, evidence-backed | ✅ |
| Salary normalisation (LPA, CTC, hourly, annual, ranges) | ✅ |
| Universal ATS scoring (8 components) | ✅ |
| Priority ranking + deadline-today guarantee | ✅ |
| Evidence-based resume tailoring + factuality gate | ✅ |
| Application lifecycle (20 states) | ✅ |
| Gmail classification, extraction, drafts, high-impact block | ✅ |
| Rejection intelligence (stated vs hypothesised) | ✅ |
| Analytics + career learning loop | ✅ |
| Agentic tool-use loop (18 tools, withheld-capability guards, audit trail) | ✅ |
| REST API (39 endpoints), dashboard, CLI | ✅ |
| Live job-board connectors | Phase 2 — interface done, integrations need API agreements |
| PDF/DOCX rendering | Phase 2 |
| Browser-assisted form filling | Phase 3 — ASSISTED only, pauses before submit |
| Multi-user auth | Phase 3 — schema ready, one seam to fill |

## Documentation

[`docs/00-index.md`](docs/00-index.md) — architecture, database schema, AI
architecture, job-source architecture, resume architecture, work-authorization
architecture, Gmail architecture, dashboard design, security architecture, the
roadmap, and agent architecture.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `CAREEROS_DATABASE_URL` | `sqlite:///~/.careeros/careeros.db` | PostgreSQL DSN for production |
| `CAREEROS_LLM` | `auto` | `off` pins the deterministic path |
| `CAREEROS_LLM_MODEL` | `claude-opus-5` | Model id |
| `ANTHROPIC_API_KEY` | — | Optional; an `ant auth login` profile also works |
| `CAREEROS_GMAIL_TOKEN` | `~/.careeros/gmail_token.json` | OAuth token (chmod 600) |
| `CAREEROS_LOCAL_MAILBOX` | — | JSON mailbox instead of Gmail |
| `CAREEROS_COUNTRIES_DIR` / `CAREEROS_TAXONOMY_DIR` | bundled | Point at your own packs |

## Tests

```bash
pytest            # 139 tests
```

The suite runs with `CAREEROS_LLM=off` throughout — the point is to prove the
product works without the model, not to mock it.

## Extending

**A new country:** drop a YAML file in `careeros/config/countries/`. Statuses,
JD signals, employment types, salary formats, boards and terminology all live
there. No Python changes.

**A new career domain:** add it to `careeros/config/taxonomy/domains.yaml`, or
just let the classifier invent it — an unfamiliar profession is filed as `other`
and, with the AI layer on, gets its own persisted domain.

**A new job source:** subclass `JobSource`, yield `RawJob`s, register it.
Dedupe, normalisation, classification and ranking are already handled.

**A new agent tool:** add a factory to `careeros/agent/tools.py` and list it in
`_FACTORIES`. Keep the output compact (it re-enters the context window), and if
it mutates anything, put its safety check *inside* the tool so the agent cannot
perform the action without it. `assert_tool_surface_is_safe()` will reject a
tool that reintroduces a withheld capability.
