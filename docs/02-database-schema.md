# 2. Database Schema

25 tables, defined in `careeros/db/models.py` (SQLAlchemy 2.0). SQLite by
default so Phase 1 needs no infrastructure; set `CAREEROS_DATABASE_URL` to a
PostgreSQL DSN and the same models apply (JSON columns map to `jsonb`).

## Map

```
                       ┌───────────────┐
                       │ career_domain │◄── seeded from YAML + inferred at runtime
                       └───────┬───────┘
                               │
  ┌──────┐   ┌────────────────┴──┐   ┌──────────────────┐
  │ user │──►│   career_track    │   │  learned_skill   │
  └──┬───┘   └───────────────────┘   └──────────────────┘
     │
     ├──► work_authorization        (one row per country)
     ├──► employer ──► evidence_item   ◄── CAREER EVIDENCE DATABASE
     ├──► certification
     ├──► education
     │
     ├──► resume_document ──► factuality_report
     ├──► cover_letter
     ├──► application ──► application_event
     │         └──────► rejection_analysis
     ├──► email_message ──► email_draft
     └──► recommendation

  ┌─────┐
  │ job │──► job_classification   (1:1)
  └──┬──┘──► priority_score        (1:1)
     ├─────► eligibility_assessment (per user)
     ├─────► match_assessment       (per career track)
     └─────► ats_assessment         (per resume version)

  pipeline_run   (audit trail for the daily automation)
```

## Design decisions worth defending

### Career domains are a table, not an enum
`career_domain.origin` is `seed` | `inferred` | `user`. The YAML seed is mirrored
in on `init`, but rows the classifier invented are never overwritten by a later
re-seed. `job_classification.domain_id` is a plain string, so a job can carry a
domain that did not exist when the code was written.

### The Career Evidence Database is the substrate
`evidence_item` stores atomic, verified facts — the brief's
*project → technology → responsibility → achievement → employer → dates* —
rather than resume prose:

| Column | Purpose |
|---|---|
| `kind` | responsibility / achievement / project / technology / certification / education / title / employment |
| `text` | the user's own wording; the only text a resume may paraphrase |
| `employer_id`, `role_title`, `project` | attribution |
| `technologies`, `skills`, `domains` | JSON tags; `skills` holds skill-graph ids |
| `start_date`, `end_date` | recency and ordering |
| `metrics` | `{"reduction_pct": 35, "sites": 30}` — figures a bullet may state |
| `verification` | verified / unverified / disputed — drives `SUPPORTED` vs `WEAKLY_SUPPORTED` |
| `strength` | 0–1, how deep the experience is; orders bullets |

A resume never stores free text alone: `resume_document.sections` is structured
JSON where each block carries `evidence_ids`, and `factuality_report.claims`
records a verdict per claim. `resume_document.is_final` is `False` whenever any
claim is unsupported — the flag is the gate, not a warning.

### Scores are stored, separately, with their inputs
`priority_score` keeps `match_score`, `eligibility_score`, `urgency_score`,
`competition_score` and the composite `overall`, plus an `explanation` array.
The UI can always answer "why is this job at the top?" without recomputation,
and a weighting change is auditable against history.

### Eligibility is per (user, job), and carries evidence
`eligibility_assessment.signals` stores the matched JD phrases and
`evidence` the quotable excerpts. `disclaimer` is stored per row rather than
rendered by the UI, so the caveat travels with the verdict through the API,
the CLI and any export.

### Rejections keep fact and hypothesis apart
`rejection_analysis` has `explicit_reason` + `explicit_quote` (only populated
when the employer actually stated one, and the quote is verified to be a real
substring of the email) and a separate `possible_reasons` JSON array where every
entry carries its own disclaimer. The two are never merged.

### Matches are per career track
`match_assessment` is keyed `(job_id, track_id)`. One posting is scored as a
Cybersecurity opportunity *and* as an SAP one; `priority_score.best_track_id`
records which won. That is what makes a single ranked list possible for a
multi-track user.

## Full table list

| Table | Rows about |
|---|---|
| `career_domain` | Extensible career taxonomy |
| `learned_skill` | Skills discovered at runtime, pending promotion to YAML |
| `user` | Master profile (personal, location, mobility, automation mode) |
| `work_authorization` | Status per country + employment preferences |
| `career_track` | Parallel career bets, each with its own targets and floor |
| `employer` | Employment history |
| `evidence_item` | **Career Evidence Database** |
| `certification` | Credentials held |
| `education` | Degrees held |
| `job` | Postings, raw + normalised |
| `job_classification` | Domain/function/seniority/skills/ATS keywords |
| `eligibility_assessment` | Work-authorization verdict + its sources |
| `match_assessment` | Skill fit per track |
| `ats_assessment` | Eight-component ATS score |
| `priority_score` | Ranking inputs + composite + explanation |
| `resume_document` | Master / track / tailored resumes |
| `factuality_report` | Per-claim traceability verdicts |
| `cover_letter` | Generated letters + evidence ids used |
| `application` | Lifecycle state |
| `application_event` | Every status transition, with its source |
| `email_message` | Classified job mail + extracted fields |
| `email_draft` | Generated replies + high-impact flags |
| `rejection_analysis` | Stated reason vs hypotheses |
| `recommendation` | "What should I apply for today?" |
| `pipeline_run` | Daily-run audit trail |

## Indexes and constraints

- `job (source, external_id)` unique; `job.fingerprint` indexed for dedupe.
- `eligibility_assessment (user_id, job_id)` unique.
- `match_assessment (job_id, track_id)` unique.
- `application (user_id, job_id)` unique.
- `work_authorization (user_id, country_code)` unique.
- `email_message.gmail_id` unique — the sync is idempotent.
- `job.country_code`, `job.deadline_on`, `job_classification.domain_id`,
  `application.status`, `email_message.category` indexed for the dashboard filters.
- SQLite runs with `PRAGMA foreign_keys=ON`; the schema relies on
  `ON DELETE CASCADE` so deleting a user removes their evidence and applications.
