# TaxVault: start here

Four commands get you from a clone to a verified installation. The last one
is the important one — run it after every deploy and every upgrade.

```bash
pip install -e '.[dev]'      # the engine, plus what the sandbox needs
taxvault verify              # prove this installation works
taxvault walkthrough         # watch one client go through it, end to end
taxvault agent --list        # the test scenarios you can run
```

`taxvault verify` exits 0 only when all 29 checks pass. It is not the test
suite: it proves **this** installation is right, on **this** machine, with
**this** configuration. A build can pass every unit test and still ship a
wheel missing a parameter file, a dependency that did not install, or a
database migrated half way.

---

## What you are looking at

```
documents in  →  agent reads them  →  return computed  →  fee quoted
                                                              ↓
   money in your account  ←  you confirm it  ←  client pays
                                                              ↓
                            prepared, ready for the client to sign
```

The agent automates eight of the nine things a preparer does. The ninth —
deciding what has to be **asked** rather than assumed — it does not automate,
because a 1099-R does not say whether the client left their job at 55 and a
1098 does not say whether the HELOC bought a kitchen or a car. It asks, with
the figure attached.

---

## Running it for real

### 1. Generate the two secrets

```bash
taxvault newkey                                       # TAXVAULT_MASTER_KEY
python -c "import secrets;print(secrets.token_urlsafe(48))"   # SESSION_SECRET
```

**The master key is the one thing you cannot lose.** Every SSN and bank
account is sealed under it. Put it in a secret manager with versioning — not
in a file on a laptop. If it is lost, that data is unreadable forever; there
is no recovery path, because the key is random and exists nowhere else.

### 2. Fill in `.env`

```bash
cp .env.example .env
```

Every setting is documented in that file. The ones that block a start:

| Setting | Without it |
|---|---|
| `TAXVAULT_MASTER_KEY` | A key is generated on disk and lost on the next restart |
| `TAXVAULT_SESSION_SECRET` | Replicas reject each other's tokens |
| `TAXVAULT_DATABASE_URL` | SQLite: no concurrent writer, no restore |
| `TAXVAULT_SMTP_*` | Nobody can sign in |
| `TAXVAULT_SMS_*` | Nobody can finish identity verification |

### 3. Migrate, then start — in that order

```bash
alembic upgrade head          # FIRST
docker compose up --build     # then the app
```

A process that starts before the migration queries a column that does not
exist yet. `init_db()` creates missing *tables* only; it cannot add a column
to a table that already exists, and it does not say so.

**`pg_dump` before every migration.** It takes seconds and a bad migration
against a season's returns has no undo.

### 4. Verify, in production mode

```bash
taxvault check --verbose      # the settings, with the remedy for each
taxvault verify               # all 29 checks, against the real database
```

`check` refuses to let the process start if production is misconfigured. That
refusal is deliberate: a deploy that fails in a log is cheap, and one that
succeeds and cannot decrypt an SSN in January is not.

---

## Getting paid

Three separate acts, and only the third moves money:

1. **You request** the fee, before filing. The client gets a reference like
   `TV-FEE-26-K7M4Q2-3` to put in the Zelle memo.
2. **The client declares** they sent it. This changes nothing — not your
   revenue, not the ledger, not the return's status.
3. **You confirm** it against your bank statement. Now it is revenue.

Step 2 is weak on purpose. Zelle gives software no way to see a transfer
arrive — your bank is the only witness, and it answers to you, not to this
application. The reference is what turns "find this payment" from guesswork
into a text search.

```bash
GET  /api/billing/payments/pending    # your confirmation queue
POST /api/billing/payments/reconcile  # match a bank export by reference
GET  /api/billing/firm/account        # what came in, what you keep
```

Reconciliation **proposes** matches rather than booking them, and a short
payment is flagged as a mismatch rather than quietly part-paid.

### On fees

Charge by **the work the return takes** — that is `config/fees.yaml`, $30 to
$550, banded by income.

**Do not charge a percentage of the refund.** Circular 230 §10.27(b)(1)
prohibits a contingent fee for preparing an original return and §10.27(c)(1)
names a refund percentage explicitly. The sanction is suspension or
disbarment from practice before the IRS, against your individual PTIN as well
as the firm. The code refuses to compute one.

---

## Before your first real client

The software is ready. These are not software:

| | Where | How long |
|---|---|---|
| **PTIN** for everyone paid to prepare | IRS online | days |
| **EFIN** to transmit returns | IRS e-Services | **45+ days** |
| **MeF acceptance testing** | IRS | weeks |
| **Written Information Security Plan** | Pub 4557; needed to renew a PTIN | a day to write |
| **State registration** | CA (CTEC), OR, NY, MD register separately | varies |

**Apply for the EFIN today.** It is the longest lead time on the list and
nothing else unblocks it. Until it arrives, the honest description is
*"prepared, ready to file"* — you file through an authorised transmitter or on
paper. The agent's final state is `ready_for_signature`; there is no `filed`
state, and a test asserts no code path can transmit.

Two more that carry penalties rather than inconvenience:

- **Form 8867 due diligence** on EITC, CTC, AOTC and head-of-household —
  **$600+ per return per credit**. The engine counts dependants but does not
  yet test whether they qualify, which is what the penalty is about. Close
  this before you file a return claiming those credits.
- **IRC 7216 consent** before using return information for anything other
  than preparing that return, including marketing. Criminal penalties.

---

## What the engine does not do yet

Listed in the order they will bite. The first two produce **wrong answers**
rather than refusing, so they come first:

1. **Schedule C expenses** — self-employment is a single net figure. No home
   office, mileage, depreciation or cost of goods. The agent asks for expenses
   as a blocker but cannot compute them.
2. **Form 8606** — IRA basis. The 1099-R parser warns when box 2b is ticked,
   but nothing computes the basis, so a client with non-deductible
   contributions is taxed twice on the same dollars.
3. Schedule E depreciation, Form 8889 (HSA), Form 2210, Form 1116
   limitation, dependant qualification tests, K-1, OCR for scanned forms.

One limitation inside what *is* built: the self-employed health insurance
deduction and the premium tax credit are circular — each affects the other.
The IRS publishes an iterative worksheet; this computes them in sequence.
Flag a self-employed client on a marketplace plan for manual review.

Full detail: `docs/22-tax-production.md`.
