# Design Documentation

Two products live in this repository. **CareerOS** is below; **TaxVault** is at
the end. They share conventions — FastAPI, SQLAlchemy, YAML-driven
configuration, deterministic engines — and no code.

## CareerOS

The system is a **Career OS**, not a resume agent. Everything is organised
around one hierarchy:

```
Country → Work authorization → Career track → Job → Eligibility
        → Priority → Resume → Application → Email → Interview → Outcome
```

It is domain-agnostic and country-agnostic by construction: a Network Engineer
job today, an SAP Security job tomorrow, a Data Scientist, a healthcare analyst,
a finance role or a veterinary practice manager next week — with no code change.

| # | Document | Covers |
|---|---|---|
| 1 | [Architecture](01-architecture.md) | Layers, the hierarchy, the three domain-agnosticism mechanisms, the daily run, degradation |
| 2 | [Database Schema](02-database-schema.md) | 25 tables, the Career Evidence Database, why domains are rows |
| 3 | [AI Architecture](03-ai-architecture.md) | Provider abstraction, Claude call shapes, where the model may decide, prompt rules |
| 4 | [Job-Source Architecture](04-job-source-architecture.md) | Connector interface, fingerprint dedupe, per-country boards, connector ethics |
| 5 | [Resume Architecture](05-resume-architecture.md) | Evidence-based assembly, tailoring rules, the factuality gate |
| 6 | [Work-Authorization Architecture](06-work-authorization-architecture.md) | Verdicts, country packs, decision order, what the system won't do |
| 7 | [Gmail Architecture](07-gmail-architecture.md) | OAuth-only access, classification, lifecycle advancement, the high-impact block |
| 8 | [Dashboard Design](08-dashboard-design.md) | Information hierarchy, the job card, deadline flags, the API behind it |
| 9 | [Security Architecture](09-security-architecture.md) | Credentials, automation limits, send gates, truthfulness as a security property |
| 10 | [Development Roadmap](10-roadmap.md) | What Phase 1 shipped, Phases 2–5, risks |
| 11 | [Agent Architecture](11-agent-architecture.md) | The tool-use loop, the 18-tool surface, and the capabilities deliberately withheld |
| 12 | [Client Architecture](12-client-architecture.md) | The universal app (iOS/Android/web from one component tree), transport security, and what is and isn't verified |

## TaxVault

US tax estimation and filing preparation: W-2 ingest, federal and state
estimates in regular or planning mode, prior-year filing checks, and payment
planning. An estimation system, not an IRS e-file provider.

| # | Document | Covers |
|---|---|---|
| 20 | [Tax Architecture](20-tax-architecture.md) | Layers, Form 1040 order of operations, regular vs planning, the 51 jurisdictions, prior years, payment |
| 21 | [Tax Security](21-tax-security.md) | SSN envelope encryption and blind index, the two gates, no-password sign-in, transport, audit trail, known limits |

## Two ways to run

`careeros.pipeline` is the scheduled, deterministic path — fixed stages, runs
with no API key, processes every job the same way every morning.
`careeros.agent` is the interactive one — Claude drives the same engines as
tools and decides what to investigate. See doc 11 for how they differ and why
the agent has autonomy over strategy but none over truthfulness.

## The two invariants

Everything above is downstream of two rules:

**Never assume sponsorship.** A posting that says nothing yields `UNKNOWN`, not
a guess. Every verdict shows the phrase that produced it and carries
"AI assessment — verify with employer/recruiter".

**Never claim experience the user does not have.** Resumes are assembled from
atomic verified evidence, every block cites its sources, and a deterministic
checker blocks any resume whose claims do not trace back.
