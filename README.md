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

## Apps

`clients/app/` is one Expo + expo-router codebase that compiles to **iOS,
Android and web** — the same screens, components and navigation on all three.

```bash
npm --prefix clients/app install

npm --prefix clients/app run export:web   # web build
careeros serve                            # served at http://127.0.0.1:8000/

npm --prefix clients/app start            # device: press i / a, or Expo Go

npm --prefix clients/app run export:demo   # self-contained build, no backend
npm --prefix clients/app run export:single # ...folded into ONE html file
```

The demo build bakes in responses captured from the real engines, so it runs on
any static host with nothing behind it. It is explicit about its limits: search
only answers recorded queries, the agent labels its reply as a recording, and
edits are not saved.

Serve the single file over http rather than double-clicking it — browsers block
the history API on `file://`, so Expo Router cannot resolve its routes there.
The file says so if you try:

```bash
python3 -m http.server 8000    # in the folder containing the file
```

Screens: **Jobs** (ranked, filters, natural-language search) · **Today**
(what to apply for + learning signals) · **Pipeline** (lifecycle) ·
**Insights** (funnel + breakdowns) · **Agent** (the loop, with its tool trace) ·
job detail with the tailor/cover-letter/prepare actions · Settings.

The clients never re-derive what the backend decides — they do not re-sort the
job list, so "deadline today ranks first" lives in exactly one place. They also
don't soften its guarantees: a resume that fails the factuality check shows
**"BLOCKED — not sendable"** with each unsupported claim, `unknown` work
authorization is never coloured green, and rejection hypotheses stay labelled
as guesses.

**Connecting a phone** — the API is localhost-only and open by default. Give it
a token before exposing it to your network:

```bash
export CAREEROS_API_TOKEN=$(openssl rand -hex 24)
careeros serve --host 0.0.0.0
```

Then enter the LAN address and the same token in the app's Settings screen.
`--host 0.0.0.0` without a token prints a warning telling you exactly this.

*Verified: the web target is built, served by the real API and driven in a
browser with zero console errors, at desktop and phone widths. The iOS and
Android targets typecheck and bundle but were **not run** — there is no mobile
simulator in the environment this was built in. See
[`docs/12-client-architecture.md`](docs/12-client-architecture.md).*

## Quick start

```bash
pip install -e .

careeros init
careeros load-profile data/sample_profile.yaml
careeros run-daily data/sample_jobs.json
careeros jobs
careeros serve                       # app at http://127.0.0.1:8000
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
| REST API (39 endpoints), CLI, zero-build dashboard | ✅ |
| Universal app: web verified in-browser; iOS/Android typecheck + bundle only | ✅ |
| Bearer-token auth + CORS policy for networked/mobile access | ✅ |
| Push notifications for deadline alerts | Phase 2 — the main reason the native target exists |
| Live job-board connectors | Phase 2 — interface done, integrations need API agreements |
| PDF/DOCX rendering | Phase 2 |
| Browser-assisted form filling | Phase 3 — ASSISTED only, pauses before submit |
| Multi-user auth | Phase 3 — schema ready, one seam to fill |

## Documentation

[`docs/00-index.md`](docs/00-index.md) — architecture, database schema, AI
architecture, job-source architecture, resume architecture, work-authorization
architecture, Gmail architecture, dashboard design, security architecture, the
roadmap, agent architecture, and client architecture.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `CAREEROS_DATABASE_URL` | `sqlite:///~/.careeros/careeros.db` | PostgreSQL DSN for production |
| `CAREEROS_LLM` | `auto` | `off` pins the deterministic path |
| `CAREEROS_LLM_MODEL` | `claude-opus-5` | Model id |
| `ANTHROPIC_API_KEY` | — | Optional; an `ant auth login` profile also works |
| `CAREEROS_GMAIL_TOKEN` | `~/.careeros/gmail_token.json` | OAuth token (chmod 600) |
| `CAREEROS_API_TOKEN` | — | Required bearer token. **Set this before serving on a network.** |
| `CAREEROS_CORS_ORIGINS` | local dev origins | Comma list; `*` honoured only when a token is set |
| `CAREEROS_LOCAL_MAILBOX` | — | JSON mailbox instead of Gmail |
| `CAREEROS_COUNTRIES_DIR` / `CAREEROS_TAXONOMY_DIR` | bundled | Point at your own packs |

## Tests

```bash
pytest                                     # 152 tests
npm --prefix clients/app run typecheck     # the app
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

**A new screen:** add a file under `clients/app/app/` — expo-router's file tree
is the route tree, and the screen works on all three platforms at once. Compose
it from `src/components/ui.tsx` so it inherits the theme and the light/dark
palette.

**A new agent tool:** add a factory to `careeros/agent/tools.py` and list it in
`_FACTORIES`. Keep the output compact (it re-enters the context window), and if
it mutates anything, put its safety check *inside* the tool so the agent cannot
perform the action without it. `assert_tool_surface_is_safe()` will reject a
tool that reintroduces a withheld capability.

---

# TaxVault

<img src="taxvault/webapp/logo.svg" alt="TaxVault — AI-powered tax filing. Secure. Accurate. Trusted." width="420">

The second product in this repository: **US tax estimation and filing
preparation**. Upload a W-2, get a federal and state estimate, compare filing
as-is against a planning scenario, find unfiled years, and price how to pay.

It is an estimation system. It is **not an IRS e-file provider** and does not
transmit returns — transmitting requires an EFIN and a Modernized e-File
connection.

```bash
$ taxvault estimate --wages 118000 --withheld 14200 --state CA \
      --status mfj --children 2 --method planning

Estimated refund of 13,147
Tax year 2025 · married_jointly · planning method

  Adjusted gross income                   118,000
  Deductions                               31,500  (standard)
  Taxable income                           86,500
  Federal tax                               5,503
  Federal refund                            8,697

  California (graduated)
    Tax                                     2,350
    Refund                                  4,450

  TOTAL                              refund 13,147
  Effective 4.66% · marginal 12%

Planning options:
  [shut]     Increase pre-tax 401(k) contributions            2,790  closes 2025-12-31
  [shut]     Top up the Health Savings Account                1,026  closes 2026-04-15
  [open] fyi Reduce over-withholding                              0
```

## Run it

```bash
pip install -e ".[dev]"
taxvault serve                 # API + the web/mobile client on :8000
```

Open <http://localhost:8000>. The same page is the desktop app and the mobile
app — it is an installable PWA, so on a phone it adds to the home screen and
runs full-screen.

To try it without configuring email and SMS delivery:

```bash
TAXVAULT_DEV_CODES=1 taxvault serve
```

One-time codes then come back in the API response instead of being delivered.
It refuses to engage against a real database, and both `/api/health` and the
sign-in screen say when it is on.

### Other commands

```bash
taxvault states                # all 51 jurisdictions, grouped by how they tax
taxvault states CA             # one state in detail
taxvault limits --year 2025    # brackets, deductions, contribution limits
taxvault payment 9400 --cannot-pay   # price every way to settle a balance
```

## What it does

| | |
|---|---|
| **Sign in** | A code to your email. No password exists to choose, forget, or steal. |
| **Verify identity** | SSN + verified email + verified mobile, before any tax data opens. |
| **Documents** | W-2 by typed boxes, pasted text, or file. A payroll-portal PDF is read and its boxes extracted; a scan is stored but reported as unread rather than guessed at. Cross-checked as it goes in — Box 4 against Box 3, Box 6 against Box 5, Box 5 less Box 1 against the Box 12 codes. |
| **Estimate** | Federal plus every state return that follows, line by line. |
| **Regular or planning** | Planning re-runs the whole calculation per strategy and shows what each is worth — and whether its deadline has passed. |
| **Prior years** | Which years are unfiled, what they owe, the penalties so far, and the three-year cut-off after which a refund is gone. |
| **Payment** | Refund routes, or every way to settle a balance priced to total cost including fees, penalty and interest. |

## States

All 51 jurisdictions, split the way it actually matters:

* **No income tax on wages (9)** — AK, FL, NH, NV, SD, TN, TX, WA, WY. Not
  simply zero: Washington taxes large long-term gains, and withholding sent to
  a no-tax state is recoverable by filing.
* **Flat rate (15)** — one rate, rarely one rule. Colorado starts from federal
  taxable income, Mississippi exempts the first $10,000, Utah swaps the
  deduction for a phasing credit, Massachusetts adds a millionaire's surtax.
* **Graduated (27)** — doubled brackets for joint filers in most, held the same
  in a few, with their own tables in NY, NJ, VT, WI and ND.

Multi-state work gets a non-resident return, a resident return taxing worldwide
income, and a credit at home for tax paid elsewhere — unless the states have a
reciprocity agreement, in which case the work state refunds in full.

## How the numbers are kept honest

* **`Decimal` everywhere.** Tax is not a domain where a float is acceptable.
* **Rates are data.** One YAML per year with a cited source, so a new tax year
  is a data review rather than a code change. 2025 carries the OBBBA changes;
  2024 is kept so an unfiled 2024 return is estimated on 2024 law.
* **Gains stack.** $40k wages plus $40k of long-term gain does not get the 0%
  rate on the gain — the wages fill the bracket first.
* **Planning is measured, not multiplied.** Each move is priced by running the
  whole return again, because a marginal rate times a contribution is wrong
  wherever a phase-out sits.
* **Tests use hand-computed figures**, worked from the published schedules
  rather than captured from a previous run.

## Security

Full detail in [docs/21-tax-security.md](docs/21-tax-security.md). The short
version: SSNs are sealed with AES-GCM under a per-field derived key bound to
their own row, with a separate keyed blind index so filing history is
searchable without ever decrypting. There are no passwords. Signing in proves
an email address; reading tax data additionally requires identity verification,
checked against the database on every request. Responses carry the last four
digits and never the number.

**Set `TAXVAULT_MASTER_KEY` from a KMS in production.** Without it a key is
generated to a 0600 file, which is right for a laptop and not for a server.

## Brand assets

`taxvault/webapp/logo.svg` is the full lockup — mark, wordmark and tagline.
`taxvault/webapp/mark.svg` is the mark alone, square, used for the favicon, the
installed app icon and the header. Both are vector, so they stay sharp from a
16px favicon to a splash screen and cost about 2KB each rather than a download.

| | |
|---|---|
| Vault navy | `#18305F` |
| Shield green | `#107A4B` |
| Bolt gold | `#C9A227` |

## Documentation

* [Tax Architecture](docs/20-tax-architecture.md)
* [Tax Security](docs/21-tax-security.md)
