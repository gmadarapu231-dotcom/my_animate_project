# 5. Resume Architecture

## The core idea: never one resume, and never freehand

Three levels of document, all generated:

```
/resumes/master/                          ← everything, from all evidence
/resumes/network-engineering/             ← one per career track
/resumes/cybersecurity/
/resumes/sap-security/
/resumes/tailored/<track>/<company>/<job-id>-v<n>.txt
```

And one substrate under all of them: the **Career Evidence Database**. A resume
is never a blob of prose that gets edited. It is *assembled* from atomic
evidence rows, and every block keeps the ids it came from.

That one decision is what makes the tailoring rules enforceable rather than
aspirational. You cannot check "did the AI invent a technology?" against a
paragraph. You can check it against a bullet that claims to have come from
evidence #7.

## The flow

```
 JD ──► classify ──► select best career track ──► select base resume
     ──► select evidence (claimable matches only)
     ──► order + rewrite bullets ──► ATS analysis
     ──► FACTUALITY CHECK ──► final resume
                  │
                  └─ any unsupported claim ⇒ is_final = False
```

## Data model

```python
Resume
 └── ResumeSection(name, kind)              summary | skills | experience | certifications | education
      └── ResumeEntry(heading, subheading, meta)     "Acme Corp" / "Senior Network Engineer" / "Mar 2021 - Present"
           └── ResumeBlock(text, evidence_ids, skills, rewritten, original_text, derived_numbers)
```

`evidence_ids` is the audit trail. `original_text` is kept whenever the AI
rewrites a block, so the rewrite can always be diffed against its source.
`derived_numbers` holds figures the *builder* legitimately states from profile
facts (total years, employer count) so the factuality checker can tell them
apart from a figure the model invented.

## What tailoring may and may not do

**May** — reorder sections; reorder bullets; rewrite bullets; improve grammar
and clarity; highlight relevant skills; adopt the posting's terminology where it
describes the same fact; combine redundant bullets; sharpen achievements;
re-pitch the summary and the skills section.

**Must not invent** — experience, employers, projects, technologies,
certifications, degrees, job titles, responsibilities, achievements, security
clearances, visa status.

Three mechanisms enforce the second list:

1. **Selection is restricted to claimable matches.** `MatchResult` marks each
   match `claimable`. DIRECT and PARTIAL-via-narrower are claimable (holding
   Azure AD really does evidence IAM). PARTIAL-via-broader and RELATED are
   **not** — they are reported to the user as adjacent experience and never
   promote evidence into the resume as proof of the requirement.

2. **The AI only rewords, and only what it can be checked against.** Rewrites
   apply only to blocks citing exactly one evidence item, so every rewrite has a
   single, unambiguous source.

3. **The factuality checker is deterministic and has the final say.**

## The factuality check

`careeros/engines/factuality.py` re-derives the entities in each final block and
asks whether the cited evidence supports each one:

| Check | Rule |
|---|---|
| Forbidden assertions | Any security-clearance or visa/work-authorization language fails, always |
| Citation | A block with no `evidence_ids` fails (except builder-generated credential lines, checked against the profile instead) |
| Skills / technologies | Every skill detected in the text must appear in the cited evidence |
| Figures | Numbers may be dropped but never invented or altered; `derived_numbers` and `metrics` are allowed |
| Degrees | Degree claims must appear in the evidence or the profile |

Verdicts: `SUPPORTED` (traces to verified evidence) · `WEAKLY_SUPPORTED` (traces
to evidence the user has not confirmed) · `REPHRASED` (reworded, facts intact) ·
`UNSUPPORTED` (blocks finalisation).

A skills line reads `"<Category>: skill, skill"`; the category is a generated
heading, so only the list after the colon is treated as a claim.

**The check never calls the model.** Its whole purpose is catching the model
inventing something.

### Worked example

Networking/security background, tailored to a Senior SAP Security Consultant
posting:

```
Assembled from 12 evidence items; 1 job skill evidenced directly.
Gaps left unclaimed (required by the posting, unsupported by evidence): firefighter
Adjacent experience NOT presented as direct: sap grc access control,
    composite roles, segregation of duties
Factuality: PASS — 20 claims checked, 20 supported, 0 unsupported.

mentions "sap"?  False      mentions "pfcg"?  False      mentions "grc"?  False
```

The resume is honest and the *user* is told exactly where the gaps are — which
is more useful than a resume that quietly claims SAP experience and fails at the
first screening question. Tested in `test_tailoring_does_not_claim_a_missing_domain`.

And when a rewrite does go wrong:

```
! UNSUPPORTED: Led Kubernetes migration reducing incidents by 80% across 200 sites
    - Mentions security clearance; the system never asserts this on a resume.
    - Technologies/skills not in the cited evidence: kubernetes.
    - Figures not supported by the evidence: 200, 80.
```

## Honest headlines

A summary headline is a claim. `_honest_headline` uses the posting's title only
when the candidate has actually held it (or held a title contained within it);
otherwise it keeps their real most recent title. Tested in
`test_headline_is_not_the_job_title_unless_actually_held`.

## The skills section is generated, never maintained

Only skills the evidence supports are listed, grouped by domain, with the
posting-relevant groups first. A hand-maintained skills list drifts from
reality; a generated one cannot.

## Output format

Plain text is the primary render — it is what ATS parsers handle most reliably,
and `AtsEngine._formatting` scores the result for parseability (labelled
sections, a contact block, bullet points, sane length, no tables or pipes). PDF
and DOCX rendering from the same structured sections is Phase 2.
