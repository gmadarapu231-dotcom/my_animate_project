# 1. System Architecture

## The reframe: a Career OS, not a resume agent

The system is not organised around resumes. It is organised around a hierarchy,
and the resume is one layer inside it:

```
Country → Work authorization → Career track → Job → Eligibility
        → Priority → Resume → Application → Email → Interview → Outcome
```

Everything above "Resume" decides *whether a job is worth your time*; everything
below decides *what you send and what happens next*. Putting country and work
authorization at the root — rather than bolting a visa filter onto a job list —
is what lets the same system run a US H1B search and an Indian full-time search
without either being a special case.

```
                    MY CAREER OS
                         │
          ┌──────────────┴──────────────┐
         USA                          INDIA
          │                             │
   ┌──────┼──────┐               ┌──────┼──────┐
  H1B    W2     C2C            Full   Contract  Remote
   │
   ├── Networking      ├── Cloud
   ├── Cybersecurity   ├── Software
   ├── SAP Security    ├── Finance
   ├── Data Eng.       ├── Healthcare
   ├── AI / ML         └── …anything else
```

The leaves of that tree are **not** an enumeration in code. Career domains are
rows in a table seeded from YAML, and the classifier creates new ones at
runtime. See §"Domain-agnosticism" below.

## Layer map

| Layer | Module | Responsibility |
|---|---|---|
| Config | `careeros/config/` | Country packs + taxonomy, as data |
| Sources | `careeros/sources/` | Fetch postings, normalise, fingerprint |
| Engines | `careeros/engines/` | Classify, eligibility, match, ATS, priority, resume, factuality, salary |
| AI | `careeros/ai/` | Pluggable LLM provider + schemas |
| Persistence | `careeros/db/` | SQLAlchemy ORM, 25 tables |
| Services | `careeros/services.py` | ORM ⇄ engine translation |
| Orchestration | `careeros/pipeline.py` | The daily run |
| Email | `careeros/gmail/` | OAuth client, classification, drafts, sync |
| Intelligence | `careeros/analytics.py`, `careeros/search.py` | Funnel, learning loop, NL search |
| Interface | `careeros/api/`, `careeros/dashboard/`, `careeros/cli.py` | REST, dashboard, CLI |

### Dependency direction

```
cli / api ──► pipeline ──► engines ──► config
                 │            ▲
                 ├──► services┘
                 ├──► sources
                 └──► ai
```

Engines never import the ORM. They take plain dataclasses, which is why every
engine is unit-testable with no database and no network, and why the whole
product runs offline.

## Domain-agnosticism: the three mechanisms

The requirement is that a Network Engineer job today, an SAP Security job
tomorrow and a veterinary practice manager next week all work — with **no code
change**. Three mechanisms deliver that:

**1. The taxonomy is a seed, not a schema.** `config/taxonomy/domains.yaml`
holds 15 domains with title patterns and weighted keywords. The classifier
scores a posting against them and normalises to a confidence share. Below the
confidence floor (0.18), the job is filed as `other` — never force-fitted into
the nearest bucket. This is tested: `test_unknown_profession_is_not_force_fitted`.

**2. New domains are persisted, not discarded.** When the LLM layer reads a job
the taxonomy cannot place, it is instructed to *invent* a snake_case
`domain_id`. `Pipeline._persist_classification` inserts it into `career_domain`
with `origin='inferred'`, and it is a first-class filter from that moment on.

**3. Keywords are derived per posting.** The ATS engine carries no industry
word list. `JobClassifier._ats_keywords` builds the keyword set from the
posting's own text, weighting the title and the "Required" section above prose.
A veterinary posting yields `veterinary`, `inventory of pharmaceuticals`,
`staff scheduling`; a SAP posting yields `sap grc access control`, `pfcg`. Same
code path. This is tested: `test_ats_keywords_are_derived_from_the_posting`.

The same applies to countries: `config/countries/*.yaml` drives work
authorization, salary parsing, employment types, terminology and job boards.
Adding Canada is a file, not a commit to Python — tested in
`test_a_new_country_needs_no_code_change`.

## The daily run

```
 fetch ──► dedupe ──► classify ──► eligibility ──► match ──► ATS
   │                                                          │
   └──────────────────────────────────────────────────────────┘
                              ▼
        priority ──► rank ──► tailor top N ──► factuality gate
                              │
                              ├──► Gmail sweep ──► advance lifecycle ──► drafts
                              └──► recommendations ──► notify
```

Each stage is idempotent and separately callable. Stage errors are collected on
the `pipeline_run` row rather than raised: one malformed posting must not stop
the morning run. Re-running the pipeline does not duplicate jobs — tested in
`test_daily_run_is_idempotent`.

## Two invariants that shape everything

**Never assume sponsorship.** Silence in a job description yields `UNKNOWN`, not
a guess, and every verdict carries the JD phrase that produced it plus an
"AI assessment — verify with employer/recruiter" disclaimer. See doc 6.

**Never claim experience the user does not have.** Resumes are *assembled* from
atomic evidence rows and every block cites the evidence ids behind it. A
deterministic factuality checker re-derives the entities in the final text and
blocks the resume if any claim does not trace back. See doc 5.

## Degradation behaviour

| Failure | Behaviour |
|---|---|
| No `ANTHROPIC_API_KEY` | Heuristic engines run; everything works, less nuance |
| LLM call fails or is declined | Logged, deterministic result stands |
| One job source down | Other sources ingest; error recorded on the run |
| Malformed posting | Skipped, error recorded, run continues |
| No Gmail token | Email layer inert; the rest is unaffected |

This is deliberate: a career agent that stops working when an API key expires is
worse than useless, because you stop checking it.
