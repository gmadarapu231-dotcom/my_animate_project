# 8. Dashboard Design

Served at `/` by the FastAPI app (`careeros/dashboard/index.html`), a single
self-contained page — no build step, no CDN, theme-aware, usable at phone width.

## Information hierarchy

The dashboard answers three questions in order of urgency:

1. **What must I do today?** → the Today tab and the deadline flags.
2. **What is worth my time?** → the ranked job list.
3. **Is my strategy working?** → Applications and Analytics.

## Tabs

| Tab | Content |
|---|---|
| **Jobs** | Ranked cards, domain and country filters, natural-language search |
| **Today** | "What should I apply for today?" + career learning signals |
| **Applications** | Lifecycle table across all 20 states |
| **Analytics** | Funnel + breakdowns by track, domain, country, visa verdict, match band, source |
| **Email** | Classified messages and reply drafts, with the high-impact holds visible |

## Filters

Built from what is actually in the database, not from a static list
(`GET /api/jobs/facets`):

```
[All domains] [Networking (4)] [Cybersecurity (3)] [SAP / ERP (1)] [Healthcare (1)] [Other (1)] …
[All countries] [United States] [India]
```

A domain the classifier invented at runtime appears here automatically, marked
with ✨ so the user can see the taxonomy growing.

## The job card

Every field the brief calls for, with the deadline driving the left border
colour:

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Senior Network Security Engineer                    🔴 DEADLINE TODAY    │
│ Cobalt Bank · Austin, TX · linkedin                                      │
│ [Networking] [Zero Trust] [senior] [US] [hybrid] [full_time]             │
│ [$140,000-$165,000 per year] [31 applicants] [posted 2026-09-11]         │
│                                                                          │
│  85.6      88.8      82.1      75        100       85                    │
│  PRIORITY  MATCH     ATS       ELIG      URGENCY   COMPETITION           │
│                                                                          │
│ ┌ Work authorization: potentially compatible ───────────────────────────┐│
│ │ Employer explicitly mentions sponsorship/transfer; an H1B transfer    ││
│ │ petition would still be required.                                     ││
│ │ Source: "we will sponsor and transfer h1b visas for exceptional…"     ││
│ │ AI assessment - verify with employer/recruiter.                       ││
│ └───────────────────────────────────────────────────────────────────────┘│
│ [Details] [Analyze] [Tailor Resume] [Cover Letter] [Prepare]             │
│ [Open posting] [Save] [Mark applied] [Reject] [Add note]                 │
└──────────────────────────────────────────────────────────────────────────┘
```

Three choices worth calling out:

**Six scores, not one.** A single "fit" number is unaccountable. Showing
priority alongside its four inputs plus ATS lets the user disagree with the
ranking — "the match is high but eligibility is unknown" is actionable in a way
that "72%" is not.

**The verdict shows its source inline.** Not behind a tooltip, not on a detail
page. The quoted JD phrase and the disclaimer are part of the card, because a
work-authorization verdict is the one output most likely to be wrong in a way
that costs the user a real opportunity.

**The original salary string is what's displayed.** `₹18 LPA CTC` renders as
`₹18 LPA CTC (~₹1,800,000/yr)` — the employer's words first, the normalisation
in parentheses. A CTC figure is not a base figure and the UI must not pretend
otherwise.

## Deadline flags

```
🔴 DEADLINE TODAY            red border      always sorted first
🟠 DEADLINE WITHIN 48 HOURS  orange
🟡 DEADLINE WITHIN 7 DAYS    amber
🟢 FUTURE                    green
⚪ NO DEADLINE               grey
⚫ EXPIRED                   dimmed, hidden by default
```

The "deadline today ranks first" rule is a sort *tier*, not a score bonus — a
mediocre job closing today appears above an excellent job closing in three
weeks, and the explanation says so: *"FINAL APPLICATION DATE IS TODAY — pinned
to the top of the list."*

Jobs ruled out by work authorization are damped and sink below everything
actionable, but stay visible: the verdict may be wrong and the user may want to
challenge it.

## Details panel

Expanding a card shows why it ranks where it does, the required and preferred
skills, the certifications the posting names, the **ATS keywords derived from
this posting** (the clearest demonstration that nothing is hard-coded per
industry), and the classification with its confidence and method.

## Search

One box, natural language, with the interpretation shown back:

```
"H1B-friendly cybersecurity jobs in Texas"
  → domain cybersecurity, country US, region TX, sponsorship available — 3 results
```

Showing the compiled filters is the point. A search box that silently
misinterprets a query is worse than one that shows its reading and lets the user
correct it.

## Accessibility and responsiveness

Light and dark palettes via CSS custom properties with a
`prefers-color-scheme` block; colour is never the only signal (every deadline
flag pairs its colour with an emoji and a text label); the layout collapses to a
single column below ~520px; tables scroll horizontally inside their own
containers rather than forcing the page to.

## API behind the UI

36 documented endpoints, browsable at `/docs`. Notable ones:

```
GET  /api/jobs?domain=&country=&bucket=       ranked cards
GET  /api/jobs/facets                         filter options, data-driven
POST /api/jobs/search                         natural-language search
POST /api/resumes/tailor                      tailor + factuality gate
POST /api/resumes/cover-letter                letter + unclaimed gaps
POST /api/applications/prepare                application packet (assisted)
POST /api/email/sync                          classify job mail, draft replies
POST /api/email/drafts/{id}/approve           409 on high-impact topics
GET  /api/recommendations                     what should I apply for today?
GET  /api/analytics, /api/analytics/learning  funnel + learning loop
POST /api/run-daily                           the whole morning run
```
