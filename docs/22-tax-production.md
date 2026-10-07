# TaxVault: going to production

Read this before taking a real client's W-2. It has three parts: what the
software needs, what the **business** needs that no software can supply, and
what is still missing from the tax engine.

The short version: the software is ready to be deployed and will compute
returns correctly. **It cannot file them**, and that is not a bug to be fixed
in a sprint — it is an IRS authorisation your firm has to hold.

---

## 1. What has to be true before the first request

`taxvault check --verbose` answers this, and the process refuses to start if
any of it is wrong when `TAXVAULT_ENV=production`. That refusal is deliberate:
a deploy that fails in a log is cheap, and one that succeeds and cannot
decrypt an SSN in January is not.

| Setting | Why it blocks |
|---|---|
| `TAXVAULT_MASTER_KEY` | **The one you cannot lose.** Every SSN and bank account is sealed under it. Without it set, a key is generated on local disk — and a container's disk does not survive a restart, so the next deploy cannot read any of them. There is no recovery: the key is random and existed nowhere else. Generate with `taxvault newkey`, store in a secret manager **with versioning**. |
| `TAXVAULT_SESSION_SECRET` | 32+ random characters, identical on every replica. Otherwise replicas reject each other's tokens and every deploy signs clients out mid-return. |
| `TAXVAULT_DATABASE_URL` | Managed PostgreSQL. SQLite has no concurrent writer, no point-in-time restore and no replication; a filing season cannot be lost to a corrupt file. |
| `TAXVAULT_SMTP_*` | Without email nobody can sign in at all. |
| `TAXVAULT_SMS_*` | Identity verification requires a **verified mobile**, so without SMS no client can finish signing up. Twilio, via `send_sms_code`. |
| `TAXVAULT_DEV_CODES` | Must be **unset**. With it on, anyone who knows a client's email can sign in as them. |
| `TAXVAULT_TRUSTED_PROXIES` | Your load balancer's address, or `*` if it is the only route in. Until set, `X-Forwarded-For` is ignored — safe, but every client is rate-limited as one. |
| `TAXVAULT_FORCE_HSTS=1` | The process sees plain HTTP behind a TLS-terminating proxy, so it cannot infer this. |
| `TAXVAULT_REDIS_URL` | Needed once `WEB_CONCURRENCY > 1`, or each worker counts rate limits separately and the real limit is multiplied by the worker count. |

### Deploy order

```bash
alembic upgrade head     # migrate FIRST
# then roll the new image
```

A process that starts before the migration queries a column that does not
exist yet. `init_db()` creates missing *tables* only — it cannot add a column
to a table that already exists, and it does not say so. See
`migrations/README.md`, particularly the warning that autogenerate renders a
**rename as drop-then-add**, which destroys a column of client data while the
tests still pass.

### Before every migration

`pg_dump` first. It takes seconds and a bad migration against a season's
returns has no undo.

---

## 2. What the business needs, which no code supplies

This is the part that decides whether you have a product. Every item is a real
authorisation or licence, and none can be written.

### You cannot file returns yet

| Requirement | How to get it | Time |
|---|---|---|
| **PTIN** for every person paid to prepare | IRS online, annual | days |
| **EFIN** to transmit | IRS e-Services application, fingerprinting, suitability check | **45+ days** |
| **MeF acceptance testing** | Against the IRS Modernized e-File system, per form | weeks |
| **Form 8879** per client, per year | Signed by the taxpayer before you transmit | per return |

Until the EFIN exists, the honest shape of the product is: TaxVault prepares
the return and produces the package, and you file through an **authorised
transmitter** or on paper. The agent's terminal state is
`ready_for_signature`, never `filed`, and `FilingGate` lists the EFIN as a
requirement marked `satisfiable_in_software: false`. A test asserts no code
path can ever transmit.

Apply for the EFIN **now**. It is the longest lead time in this list and
nothing else unblocks it.

### Other obligations

- **Written Information Security Plan.** IRS Publication 4557 requires one;
  the FTC Safeguards Rule makes it enforceable. You cannot renew a PTIN
  without attesting to it.
- **Due diligence (Form 8867)** on EITC, CTC, AOTC and head-of-household.
  **$600+ penalty per return per credit** for failing it, and it is the most
  commonly assessed preparer penalty there is.
- **Consent under IRC 7216** before using or disclosing return information for
  anything other than preparing the return — including using it to market
  another service. Criminal penalties, not civil.
- **E-file provider rules (Pub 3112)** once you have an EFIN.
- **State registration.** California (CTEC), Oregon, New York and Maryland
  each register preparers separately.

### Prior-year filing checks are inert

`IRSTranscriptProvider` raises by design. Real transcript access needs IRS
e-Services plus a signed **Form 8821** or **2848** per client. Until then the
prior-year screen works from what the client tells you, and says so.

### Do not hold client tax money

The remittance engine keeps `bucket="fee"` and `bucket="tax"` separate and
refuses to disburse more than it holds, but code cannot grant a licence.
Holding client money to pay the IRS on their behalf is **money transmission**
and needs state-by-state licensing. The shipped default is the safe one: the
client pays the IRS directly and you collect only your fee.

---

## 3. Fees: what you may and may not charge

**A percentage of the refund is prohibited.** Circular 230 §10.27(b)(1) bars a
contingent fee for preparing an original return, and §10.27(c)(1) names "a
percentage of the refund" as exactly that. The sanction is suspension or
disbarment from practice before the IRS, against the individual's PTIN as well
as the firm. Several states ban it outright.

`quote_platform_fee` raises on it rather than computing one. The model stays in
`commission.yaml` listed as refused, with the citation, so the question is
answered once.

**What works instead**, all quotable before the work starts:

- **Complexity tiers** — `fees.yaml`, $30 to $550, banded by income. This is
  the main event and it already prices every return the engine computes.
- **Per return** — a flat platform fee charged to the firm, with volume breaks.
- **Revenue share** — a percentage of the *preparation fee*, which touches no
  refund.
- **Subscription** — per seat, with an overage.

Taking a fee out of a refund is refused separately: that is a refund transfer,
needing a bank partner and money transmitter licensing, and it is the
mechanism behind most fee complaints the IRS receives.

On a $4,000 refund a 15% cut is $600. The complexity fee for the kind of
return that produces a $4,000 refund is usually more than that, arrives
whether the return refunds or owes, and can be quoted up front.

---

## 4. What the tax engine still does not do

Covered and tested: W-2, 1099-B, 1099-DIV, 1099-R, 1098, 1095-A, 1099-INT,
1099-NEC, 1099-MISC, 1099-K, 1099-G, SSA-1099, 1098-E, 1098-T. Federal
1040 with Schedule A, B, D, SE, 8995 (QBI), 6251 (AMT), 8962 (premium tax
credit), 5329 (early distributions), plus all 51 jurisdictions.

Still missing, in the order they will bite:

| Gap | Who it affects |
|---|---|
| **Schedule C detail** | `self_employment_income` is net profit with no expense structure: no home office, no mileage, no depreciation, no cost of goods. The agent asks for expenses as a blocker, but it cannot compute them. Self-employed clients are a large share of a practice's revenue. |
| **Form 8606** | IRA basis, non-deductible contributions, backdoor Roth. The 1099-R parser **warns** when box 2b is ticked, but nothing computes the basis — so a client with non-deductible contributions is taxed twice on the same dollars. |
| **Schedule E detail** | `rental_income` is one number. No depreciation, no passive activity loss limits, no at-risk rules. |
| **Form 8889** | HSA contribution limits and distributions are not checked. |
| **Form 2210** | Current-year underpayment penalty. `engines/compliance.py` handles prior years; the current year does not compute one. |
| **Form 1116** | Foreign tax credit is taken as a flat amount with no limitation. |
| **Dependent qualification** | Dependants are counted, not tested. Head-of-household and qualifying-child tests are what Form 8867 due diligence is **about**, so this interacts with the $600-per-return penalty above. |
| **K-1** | Partnership and S-corporation income. |
| **OCR** | A scanned or photographed form reads as nothing. Deliberate — a guessed figure is worse than a blank, because a blank gets asked about — but it needs a vendor before clients can photograph forms. |
| **Form 5695** | Residential energy credits. |

### Known limitation in what *is* built

The self-employed health insurance deduction and the premium tax credit are
**circular**: each affects the other. The IRS publishes an iterative
worksheet. This engine computes them in sequence, which is close but not
exact for a self-employed client on a marketplace plan. Flag such returns for
manual review.

---

## 5. A realistic sequence

1. **Now** — apply for the EFIN. Longest lead time, blocks nothing else.
2. **Now** — write the Written Information Security Plan. Required for the
   PTIN renewal.
3. **Week 1** — deploy to staging with `TAXVAULT_ENV=staging`, real
   PostgreSQL, real secrets. Run `taxvault check --verbose` until clean.
4. **Week 1** — `taxvault agent --list` and run every scenario. Confirm the
   figures against your own worked examples before trusting one.
5. **Weeks 2-4** — close the Schedule C and Form 8606 gaps. They are the two
   that silently produce wrong answers rather than refusing.
6. **Before any real client** — dependent qualification tests, because Form
   8867 due diligence carries a per-return penalty.
7. **When the EFIN arrives** — MeF acceptance testing, then wire the
   transmitter behind the existing `FilingGate`.

Until step 7, say "prepared, ready to file" and not "filed". The gate already
enforces it; the marketing has to match.
