# 10. Development Roadmap

## Phase 1 — Foundation ✅ shipped in this repository

The hierarchy end to end, domain-agnostic and country-agnostic, running with no
API key and no external service.

| Area | Status |
|---|---|
| Country packs (USA, India) as data | ✅ |
| Extensible career taxonomy (15 seed domains, runtime-extensible) | ✅ |
| Skill graph (95 nodes) + cross-domain matching | ✅ |
| Database schema (25 tables) | ✅ |
| Career Evidence Database + profile import/export | ✅ |
| Job classification (heuristic + optional LLM) | ✅ |
| Work-authorization engine with evidence + disclaimers | ✅ |
| Multi-country salary normalisation (LPA/CTC/hourly/annual) | ✅ |
| Universal ATS engine (8 components) | ✅ |
| Priority engine + deadline-today guarantee | ✅ |
| Evidence-based resume assembly + tailoring | ✅ |
| Deterministic factuality gate | ✅ |
| Cover letters (gaps reported, not papered over) | ✅ |
| Application lifecycle (20 states) + event history | ✅ |
| Gmail OAuth client, classification, extraction, drafts | ✅ |
| High-impact send block | ✅ |
| Rejection intelligence (stated vs hypothesised) | ✅ |
| Natural-language search | ✅ |
| Analytics + career learning loop | ✅ |
| Daily pipeline (idempotent, error-tolerant) | ✅ |
| REST API (36 endpoints) + dashboard + CLI | ✅ |
| 104 tests, all passing offline | ✅ |

**Deliberately not in Phase 1**, and why:

- **Live job-board connectors.** The connector interface and the per-country
  board declarations are done; the actual integrations need API agreements, not
  code. `JsonFileSource` is the working path meanwhile.
- **Browser-driven application submission.** Phase 3, under the constraints in
  doc 9.
- **PDF/DOCX rendering.** Plain text is what ATS parsers handle best, and the
  structured sections make rendering a formatting task rather than a rewrite.
- **Multi-user auth.** The schema is ready (`user_id` everywhere); the seam is
  `current_user`.

## Phase 2 — Real job flow (≈ 4 weeks)

1. **Greenhouse / Lever / Ashby connectors.** Public board APIs, no auth,
   per-company. The lowest-risk real data.
2. **Email-alert ingestion.** Job-alert emails already arrive in the mailbox the
   Gmail layer reads — parse them into `RawJob`s. Highest signal per unit of
   effort, and no terms-of-service question.
3. **Manual paste.** A URL or pasted description in the dashboard.
4. **Scheduler.** The daily run on cron/APScheduler, with push/email notification
   of deadlines today.
5. **PDF + DOCX rendering** from the existing structured sections.
6. **Track-level base resumes** materialised per career track, not only master
   and tailored.
7. **PostgreSQL** + Alembic migrations.

## Phase 3 — Assisted applications (≈ 6 weeks)

1. **Form-filling assistant.** Playwright, ASSISTED mode only: open, fill known
   fields, attach the resume and letter, **pause before submission**. Never
   touches CAPTCHA, MFA or bot protection.
2. **Screening-question answering** from the Master Profile, with immigration
   questions always deferred to the user.
3. **Follow-up automation.** Detect stalled applications and draft a follow-up —
   drafted, approved, then sent.
4. **Interview preparation.** Per-job brief assembled from the JD and the
   evidence database: likely questions, which evidence answers each, gaps to
   prepare for honestly.
5. **Multi-user auth** and per-user encryption.

## Phase 4 — Career intelligence (≈ 6 weeks)

1. **Outcome-weighted ranking.** Feed the learning loop back into the priority
   weights — if Cybersecurity roles naming IAM + Azure + SIEM convert at a
   higher interview rate, weight similar jobs up. Requires enough history to be
   honest about (the current floor is 10 applications per group before a rate is
   presented as a finding).
2. **Skill-gap planning.** The learning loop names gaps; Phase 4 sequences them
   into a plan with real courses and certifications, scored by how many targeted
   jobs each unlocks.
3. **Market intelligence.** Salary bands, demand trends and competition by
   domain, country and seniority, from observed postings.
4. **A/B testing resumes.** Two tailored variants, tracked to outcome.

## Phase 5 — More countries, more people (≈ 8 weeks)

1. **Canada, UK, Australia, Singapore, UAE, Germany.** Each is a country pack
   plus its own board connectors; the engines do not change.
2. **Localisation.** Country terminology is already in the packs
   (`notice_period`, `resume_noun`, `region_noun`); add UI translation.
3. **Hosted multi-tenant deployment** with the doc 9 requirements.
4. **Mobile app.**

## Risks worth tracking

| Risk | Response |
|---|---|
| Job boards restrict automated access | Lead with official APIs and email-alert ingestion; never scrape against terms |
| LLM cost grows with volume | Cache the stable prefix, classify only new jobs, tailor only the top N — already in place |
| Classification quality on rare professions | The `other` bucket plus runtime domain creation means quality degrades gracefully rather than silently mis-filing |
| A tailored resume overstates | Deterministic factuality gate; a resume with any unsupported claim cannot be marked final |
| Users over-trust eligibility verdicts | `UNKNOWN` default, evidence shown inline, disclaimer stored on every row |
| Analytics mislead on small samples | Rates below 10 applications are labelled low-confidence and excluded from track comparisons |

## Definition of done for Phase 2

- Three live board connectors ingesting daily without manual intervention.
- Email-alert ingestion producing jobs indistinguishable from API-sourced ones.
- The daily run scheduled, with a notification that names deadlines closing today.
- PDF export that a human would send.
- Postgres in production with migrations.
