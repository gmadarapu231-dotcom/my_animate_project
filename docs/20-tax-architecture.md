# TaxVault — Architecture

TaxVault is the second product in this repository. It shares the conventions of
CareerOS — FastAPI, SQLAlchemy 2.0, YAML-driven configuration, no mandatory AI
— and shares none of its code, because tax and careers have nothing in common
but a database driver.

It is an **estimation and preparation** system. It is not an IRS e-file
provider and does not transmit returns. That boundary is stated in
`/api/health`, in the client footer, and in the docstrings of the two modules
where somebody might otherwise assume more.

```
Account ─ sign-in (email code, no password)
   └─ Taxpayer ─ identity (SSN sealed + blind index, email + mobile verified)
        ├─ TaxDocument ── W-2 boxes, validated on the way in
        ├─ Estimate ───── regular | planning, federal + every state
        │     └─ PaymentPlan ── refund route, or how a balance gets settled
        └─ FilingRecord ─ what is on record for earlier years, and its source
```

## Layers

| Layer | Module | Responsibility |
|---|---|---|
| Money | `taxvault/money.py` | `Decimal` everywhere, bracket arithmetic, phase-outs |
| Parameters | `taxvault/config/` | One YAML per tax year; 51 jurisdictions; IRS payment options |
| Crypto | `taxvault/crypto.py` | AES-GCM envelope encryption, blind index, SSN validation |
| Forms | `taxvault/forms/` | W-2 boxes and cross-checks, PDF text extraction, layout reading |
| Engines | `taxvault/engines/` | federal, state, planning, compliance, payments, estimate |
| Auth | `taxvault/auth/` | One-time codes, session tokens, identity verification |
| API | `taxvault/api/` | 26 paths / 28 operations, two gates, rate limiting, security headers |
| Client | `taxvault/webapp/` | One responsive PWA for desktop web and mobile |

## Three rules that run through it

### 1. Rates are data, not code

Every figure lives in `taxvault/config/federal/<year>.yaml` or
`taxvault/config/states/<year>.yaml` with a `source` and an `as_of`. A new tax
year is a new file and a data review, not a code change. 2025 carries the
OBBBA changes; 2024 is kept intact so an unfiled 2024 return is estimated on
2024 law rather than on today's.

This is also why `taxvault limits` and `taxvault states` exist: the person reviewing
the tables in January should not have to read Python to check them.

### 2. Order of operations is not negotiable

`compute_federal` follows Form 1040 exactly, because almost every interesting
quantity gates something later:

```
total income
  → adjustments → AGI                    (AGI gates a dozen phase-outs)
  → deduction, compared after AGI        (medical floor, charitable limit are % of AGI)
  → QBI, limited by taxable income
  → taxable income
  → ordinary tax + preferential tax      (gains STACK above ordinary income)
  → AMT comparison
  → non-refundable credits (to zero)
  → other taxes: SE, Additional Medicare, NIIT
  → refundable credits (beyond zero)
  → balance
```

The stacking rule deserves its own sentence. A client with $40,000 of wages and
$40,000 of long-term gain does not pay 0% on the gain because $40,000 sits
under the 0% breakpoint — the wages fill the bracket first. Getting this
backwards flatters an estimate by thousands of dollars, so it is implemented
explicitly in `_preferential_tax` and pinned by test.

### 3. Every number keeps its derivation

`FederalResult.lines` records each line with the form it comes from.
`Estimate.breakdown` persists it. The client can show the client their own
return line by line. An estimate nobody can reconstruct is an estimate nobody
can defend.

## Reading a W-2

A W-2 is a grid, and that is the whole difficulty. Flattening a PDF to text
keeps the labels and the figures but throws away which box each figure was in,
reducing the association to "roughly adjacent". On a real form that is wrong
often enough to matter: boxes 3 and 4 share a visual line, so a reader anchored
to the start of that line gives both the same figure, and boxes 16 and 17 end
up far apart in the dump, so the state tax reads as zero and the client is
shown a state refund that does not exist.

So `taxvault/forms/layout.py` keeps the coordinates, and
`taxvault/forms/w2_layout.py` asks a geometric question instead of a textual
one: *what amount is drawn inside this label's box?* Labels are anchored where
the match begins rather than where the line begins, which is what keeps box 1
from resolving to the identity box printed beside it.

Three strategies, best first, and the response says which one ran:

| Strategy | When | Confidence |
|---|---|---|
| `layout` | a PDF whose geometry is readable | fraction of critical boxes located |
| `text` | geometry unreadable, flat text usable | fraction of critical boxes matched |
| `repaired` | labels and amounts wholly separated | cut by 20%, routed to review |

Names come out of the same geometry. The employer and employee blocks hold a
name on the first line and an address underneath, so reading the block means an
address line is never mistaken for a company. `tidy_name` then cases it like a
name rather than calling `str.title()`, which renders "NORTHWIND LOGISTICS LLC"
as "Northwind Logistics Llc".

Whatever comes out is shown back to the client as editable fields before it
drives anything, with any box that could not be found marked rather than left
looking like a zero the form actually stated. An estimate is only as good as
the figures under it, and the client is the only one who can confirm them.

## Regular versus planning

Both modes run the same engine on the same figures.

**Regular** is the return as the documents stand.

**Planning** re-runs the whole calculation once per strategy and takes the
difference. It does not multiply a marginal rate by a contribution, because
that shortcut is wrong wherever a phase-out sits: a $5,000 traditional IRA at
$85,000 of income can be worth far more than 22% of $5,000 if it drags AGI
under a credit threshold, and worth nothing if tax is already zero.

Each strategy carries the date its window closes:

| Window | Closes | Examples |
|---|---|---|
| `year_end` | 31 Dec of the tax year | 401(k), dependent care FSA, charitable bunching, loss harvesting |
| `filing_deadline` | 15 Apr after | HSA, traditional IRA, Saver's Credit |
| `extended_deadline` | 15 Oct with an extension | SEP-IRA, solo 401(k) funding |
| `election` | while the return can be amended | filing status, itemise or not |

This is the difference between a planning screen that is useful and one that is
decorative. Most tools list ideas without mentioning that the 401(k) window
shut on 31 December. Here, a client filing in September sees those moves under
"Missed for 2025", with the figures labelled *would have saved*, and a note
telling them to act on the list for next year.

Strategies are also split into **actionable** and **informational**. "Your
state takes $8,000 a year" is true and worth knowing, but it is not a move, and
ranking it above a fundable action because its headline number is bigger would
mislead.

## States

Three shapes, and the client screen shows which one applies:

* **none** (9): AK, FL, NH, NV, SD, TN, TX, WA, WY. Not simply "zero" —
  Washington levies a 7% excise on large long-term gains, and withholding paid
  to a no-tax state is recoverable by filing.
* **flat** (15): one rate, rarely one rule. Colorado starts from federal
  taxable income; Mississippi exempts the first $10,000; Utah replaces the
  deduction with a credit that phases out; Massachusetts adds a 4% surtax.
* **graduated** (27): a schedule, doubled for joint filers in most states, held
  the same in a handful (the marriage penalty), or given its own table (NY, NJ,
  VT, WI, ND).

Multi-state work is modelled properly: a non-resident return for the work
state, a resident return at home taxing income from everywhere, and a credit at
home for tax paid elsewhere — unless the two states have a reciprocity
agreement, in which case the work state owes nothing and refunds its
withholding in full.

The starting figure is Box 16 where a W-2 reports it, not federal AGI. In New
Jersey and Pennsylvania those differ by the whole 401(k) deferral.

## Prior years

`check_filing_history` answers "has this SSN filed?" — with the source of every
claim attached, because *"we could not find a 2023 return"* and *"the IRS
confirms none was filed"* are very different sentences.

| Source | Means |
|---|---|
| `irs_transcript` | IRS Transcript Delivery System, under a signed 8821/2848 |
| `state_portal` | A state revenue department |
| `client_stated` | What the client told us |
| `inferred` | A W-2 on file for a year with no return recorded |

`IRSTranscriptProvider` is deliberately inert: it raises an error naming what
is needed. Returning a plausible-looking empty history that a preparer might
read as "nothing was ever filed" would be worse than refusing.

Penalties follow the statute, including the interaction that makes hand
estimates wrong: in any month where both run, the 5% failure-to-file penalty is
reduced by the 0.5% failure-to-pay penalty. A year that owed nothing carries no
penalty at all — but its refund is forfeited three calendar years after the due
date, and that clock is the most urgent thing on the screen.

## Payment

Every route is priced to a **total cost**, because a 72-month plan with a
comfortable monthly figure can cost more in penalty and interest than a short
one. A direct-debit instalment agreement halves the failure-to-pay penalty
(0.5% → 0.25%), which is real and is modelled. Paying by card costs ~1.85% for
no tax benefit, and is shown with the fee attached.

An extension extends the time to **file**, never the time to **pay** — stated
wherever it could be misread.

## What is deliberately absent

* **E-filing.** Needs an EFIN and an MeF connection.
* **OCR.** A PDF that carries text -- which is what every payroll portal
  emits -- is read, and its boxes come straight out. A scan or a photograph
  carries pixels instead, and reading those needs OCR this server does not
  run: the file is stored encrypted, the confidence comes back at zero, and
  the response says plainly that it was not read. Silently contributing
  nothing to an estimate that looks complete would be the worst failure this
  system could have.
* **Real IRS identity proofing.** The three-factor check confirms the SSN
  matches the account and that codes reached the email and phone on file. It is
  not knowledge-based authentication against a bureau, and says so.
* **An AI layer.** Nothing here needs one. Tax is arithmetic over a statute,
  and a language model is the wrong tool for a figure that has to be exactly
  right.
