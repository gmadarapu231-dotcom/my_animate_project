# 7. Gmail Architecture

## Access model

**OAuth only.** There is no field anywhere in the system for a Gmail password,
and no code path that would accept one. `careeros gmail-auth` runs Google's
installed-app consent flow and stores the resulting refresh token.

**Least privilege.** The default scopes are:

```
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.compose     ← create drafts
```

`gmail.send` is **not** requested unless the user passes `--allow-send` and
re-runs consent. Even then, the draft workflow still gates every send.

**Token storage.** `~/.careeros/gmail_token.json`, chmod 600, outside the
application database and outside the repository. Path overridable with
`CAREEROS_GMAIL_TOKEN`.

## Client interface

```python
class GmailClient(Protocol):
    name: str
    can_send: bool
    def list_messages(self, query, limit) -> Iterator[MailMessage]: ...
    def create_draft(self, thread_id, to, subject, body) -> str: ...
    def send(self, thread_id, to, subject, body) -> str: ...
```

Two implementations: `OAuthGmailClient` (real Gmail) and `LocalMailboxClient`
(a JSON file, `can_send = False`, `send()` raises). The local client is how the
classification and drafting layers are developed and tested with no Google
project at all, and it is what the test suite uses.

## Scoping the sweep

The agent reads job mail, not the inbox:

```
newer_than:30d (
  subject:(application OR interview OR recruiter OR opportunity OR assessment
           OR offer OR position)
  OR from:(greenhouse.io OR lever.co OR myworkday.com OR icims.com
           OR ashbyhq.com OR smartrecruiters.com)
)
```

## Classification

Nine categories: `recruiter`, `interview`, `assessment`,
`application_confirmation`, `rejection`, `offer`, `sponsorship`, `follow_up`,
`other`.

Signatures are evaluated in a fixed order so precedence is explicit — a
rejection that also says "we'll keep your resume on file" is still a rejection,
and an offer outranks everything. Confidence rises with the number of
independent signatures that fire, capped at 0.95.

Extracted per message: company, position, recruiter name and email, job URL,
interview date/time, assessment deadline, required action, application status
hint, and — only when actually stated — a rejection reason with its verbatim
quote.

When the LLM layer is available it refines all of this, with one guard: an
LLM-supplied `rejection_reason_explicit` is accepted **only if** its
`rejection_reason_quote` is a real substring of the email. That is the field
most likely to be confabulated, and it feeds the analytics the user will make
decisions from.

## Driving the lifecycle

A classified message maps to a status and advances the linked application,
recording an `application_event` with `source='email'`:

| Category | Status |
|---|---|
| `application_confirmation` | `application_received` |
| `recruiter` | `recruiter_contact` |
| `interview` | `interview` |
| `assessment` | `assessment` |
| `offer` | `offer` |
| `rejection` | `rejected` |

Terminal statuses (`accepted`, `rejected`, `withdrawn`) are never walked back by
an inferred signal.

**Linking is deliberately conservative.** A message is attached to an
application by scoring company match (3), title match (2) and exact job-URL
match (5), and requires ≥ 3. An unmatched email stays unlinked rather than being
guessed onto the nearest application — a wrong link corrupts the funnel
analytics the learning loop depends on.

## Replies: DRAFT → APPROVAL → SEND

Eight reply intents (recruiter interest, interview availability, assessment
acknowledgement, application acknowledgement, follow-up, thank-you after
rejection, offer acknowledgement, sponsorship enquiry). Templates always work;
the LLM rewrites them when available.

### The high-impact block

Seven topics are never auto-sent, regardless of the user's automation mode:

```
immigration · sponsorship_commitment · salary_negotiation
contract · offer · resignation · legal
```

Detection runs on **both** the incoming message and the generated reply — a
reply can raise a topic the incoming message did not. When any fires, the draft
state becomes `BLOCKED_HIGH_IMPACT`:

- `/approve` returns **409**, with an explanation.
- `/send` refuses.
- Editing a draft **re-runs** the check, so a human edit that introduces a
  salary figure re-blocks it.

Tested in `test_high_impact_reply_is_blocked_even_in_automated_mode`,
`test_email_sync_classifies_and_blocks_high_impact_drafts` and
`test_editing_a_draft_re_runs_the_high_impact_check`.

Even ordinary replies never auto-send in Phase 1: `may_auto_send` requires state
`APPROVED` **and** no high-impact topics, and approval is a human action.

## Rejection intelligence

`rejection_analysis` keeps two things apart and never merges them:

- `explicit_reason` + `explicit_quote` — what the employer actually said.
- `possible_reasons` — the system's hypotheses, each with its basis (match
  score, eligibility score, applicant count), a confidence, and its own
  disclaimer: *"Hypothesis generated by CareerOS — not a reason given by the
  employer."*

## Privacy

Stored per message: Gmail id, thread id, sender, subject, a snippet, the
category and the extracted fields. Full bodies are **not** persisted — they are
classified in memory and discarded. See doc 9 for retention and revocation.
