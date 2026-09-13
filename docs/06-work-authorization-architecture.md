# 6. Work-Authorization Architecture

## Four rules, enforced in code

1. **Never assume sponsorship.** Silence in a posting yields `UNKNOWN`.
2. **Always show the source.** Every verdict carries the JD phrase that produced
   it, with a quotable excerpt.
3. **No hard-coded immigration law.** Statuses, the phrases that matter and what
   each implies live in the country pack. `workauth.py` applies the pack's
   rules; it encodes no legal conclusions of its own.
4. **Always disclaim.** Output is an AI assessment and says so, on every row.

## Verdicts

| Verdict | Meaning | Score |
|---|---|---|
| `COMPATIBLE` | Status needs no employer sponsorship to start | 100 |
| `POTENTIALLY_COMPATIBLE` | Employer explicitly mentions sponsorship/transfer; a petition is still required | 75 |
| `UNKNOWN` | Posting says nothing. **The default.** | 50 |
| `NOT_COMPATIBLE` | Employer states a requirement the status does not satisfy | 0 |

`UNKNOWN` scoring 50 rather than 0 is deliberate: most postings are silent, and
treating silence as rejection would hide most of the market. It scores below
`POTENTIALLY_COMPATIBLE` because a stated willingness to sponsor is real
information and silence is not.

## The country pack

```yaml
work_authorization:
  statuses:
    - id: h1b
      label: H1B (current holder)
      needs_sponsorship_now: true      # a transfer is a petition
      needs_sponsorship_future: true
      satisfies: [any_authorized]
      transfer_required: true
    - id: green_card
      label: Permanent Resident / Green Card
      needs_sponsorship_now: false
      needs_sponsorship_future: false
      satisfies: [permanent, any_authorized]

  jd_signals:
    permanent_status_required:
      severity: blocking
      requires: permanent
      patterns: ["us citizen or green card", "citizens or permanent residents", …]
    sponsorship_unavailable:
      severity: blocking_if_needs_sponsorship
      patterns: ["no sponsorship", "unable to sponsor", "will not sponsor", …]
    sponsorship_available:
      severity: enabling
      patterns: ["will sponsor", "h1b transfer", "sponsorship available", …]
```

The USA pack ships 13 statuses (US Citizen, Green Card, EAD, H1B, H1B Transfer,
H1B Sponsorship Required, OPT, STEM OPT, TN, L1, O1, Other, Unknown) and 8
employment types (W2, C2C, C2H, Contract, Full-Time, Part-Time, Temporary,
Internship). The India pack ships its own statuses (Indian Citizen, OCI, PIO,
Employment Visa, Other, Unknown) and types (Full-Time, Contract, C2H,
Internship, Part-Time, Freelance).

`satisfies` is the join: a posting demands a *capability* (`citizen`,
`permanent`, `any_authorized`) and a status either provides it or does not. No
status names another status.

## Decision order

```
1. Hard status requirements       "must be a US citizen"    → NOT_COMPATIBLE
2. Clearance                      "active clearance required"
                                  → NOT_COMPATIBLE unless the profile records one
3. Sponsorship
   status needs sponsorship now?
     ├── employer says unavailable  → NOT_COMPATIBLE
     ├── employer says available    → POTENTIALLY_COMPATIBLE (petition still needed)
     └── employer silent            → UNKNOWN  ("sponsorship is NOT assumed")
   status needs no sponsorship?
     └── COMPATIBLE (+ note if the status has a future expiry)
4. Employment type: a preference mismatch, never a block (−15 to the score)
```

Employment type deserves its own note. A W2-preferring candidate looking at a
C2C posting is a *mismatch*, not an ineligibility — the arrangement is often
negotiable, and hard-blocking it would hide good jobs. It costs score and is
stated in the reasons.

Clearance is treated as blocking *only when the profile does not record one*,
because the system cannot verify a clearance and should ask rather than assume.

## Evidence, always

```
h1b + "We are unable to sponsor visas for this position."
  → not_compatible
    reason: Employer explicitly states sponsorship is not available and your
            status requires it.
    source: sponsorship_unavailable
            "we are unable to sponsor visas for this position."
    AI assessment - verify with employer/recruiter.
```

The excerpt is stored on the assessment row, so it travels through the API, the
dashboard and any export. A user can always see *why* a job was ruled out, and
overrule it — the verdict de-prioritises a job (×0.25) but never hides it.

## India

The same engine, a different pack. Employment types run Full-Time / Contract /
C2H / Internship / Part-Time / Freelance; the signals cover
"candidates must be authorized to work in India"; salary parsing understands
LPA, CTC, lakh, crore and monthly figures (doc 2 and `engines/salary.py`).

## Adding a country

Drop a YAML file in `config/countries/`. No Python changes — proven by
`test_a_new_country_needs_no_code_change`, which builds a working Canada pack
inside the test. The planned set is Canada, UK, Australia, Europe, Middle East
and Singapore.

## What this system does not do

It does not give immigration advice, determine eligibility for a visa category,
interpret an individual's case, or replace an attorney. It reads what a posting
says, compares it to what the user recorded, and shows its work. Every output
carries "AI assessment — verify with employer/recruiter", and the profile
disclaims further: *"Do not state a visa status on an application that differs
from your Master Profile."*

The resume layer enforces the other half of this: visa and clearance language is
on the factuality checker's forbidden list, so no generated resume can ever
assert an immigration status at all.
