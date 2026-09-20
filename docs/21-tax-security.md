# TaxVault — Security

A tax preparer's client list is one of the highest-value targets there is: a
full name, address, date of birth, employer, income and Social Security number
for every client, in one place. The design starts from that.

## The thing that cannot be rotated

A Social Security number is not a password. It cannot be changed, it is a
lifetime identifier, and a breach of one is permanent. So it is never stored in
the clear, never logged, never returned by the API, and never used as a
database key.

### Envelope encryption

`taxvault/crypto.py`, AES-256-GCM:

* **Master key** from `TAXVAULT_MASTER_KEY` — injected from KMS or Secrets Manager
  in a cloud deployment. On a developer machine it is generated once into a
  0600 file, so the default is a working encrypted system rather than an
  unencrypted one. Security you have to switch on is security nobody switches
  on.
* **Per-field data keys**, derived by HKDF over the field's purpose. Reading an
  SSN ciphertext with `purpose="bank"` fails.
* **Row binding via AAD.** Each ciphertext is authenticated against
  `taxpayer:<id>`. Copying an SSN ciphertext from one row to another fails to
  decrypt rather than silently succeeding — which defeats the attack where a
  writeable database is used to swap identities.
* **Non-deterministic.** Two encryptions of the same SSN differ, so the
  ciphertext does not leak equality.
* **Key id per ciphertext**, so the key can be rotated without a flag day.

### The blind index

"Has this SSN filed before?" has to be answerable without decrypting anything.
`ssn_index` is an HMAC-SHA256 of the normalised digits under a key that is
*not* the encryption key:

* deterministic, so it is searchable;
* keyed, so a stolen database cannot be brute-forced against the ~10⁹ possible
  SSNs the way a bare SHA-256 could.

### Validation

`normalise_ssn` rejects the ranges the SSA never issues — area 000, 666,
900–999 for an SSN; group 00; serial 0000 — and recognises ITINs in the
published 9xx ranges rather than rejecting a resident alien's valid number.

`TaxId.__str__` and `__repr__` both return the masked form, so an SSN cannot
reach a log through an f-string. A `RedactingFilter` on the root logger strips
anything SSN-shaped as a second line of defence.

## Two gates, not one

Conflating these is the mistake worth avoiding:

| Gate | Proves | Grants |
|---|---|---|
| **Sign-in** | control of an email address | an empty account |
| **Identity** | SSN + verified email + verified mobile | tax data |

The session token carries `identity_verified` as a separate claim, and
`resolve_session` re-checks it against the database on every request rather
than trusting the token — so revoking verification takes effect immediately
instead of whenever the token happens to expire.

`verified_account` is the dependency for every route touching tax data.
`current_account` is only for the routes that set identity up.

### Honest about what it proves

Matching an SSN against a stored blind index tells us the number is the one
already held for this account. It does not tell us the person typing it owns
it. The API says so in the response, rather than implying a government identity
check it did not perform.

## No passwords, anywhere

There is no password column. A password is reusable, phishable, and ends up in
a breach dump next to the same person's SSN. Sign-in is a one-time code to a
channel the client already controls.

Codes are hashed with the destination mixed in, so a hash lifted from the
database cannot be replayed against a different address. Attempts are capped at
5 — a six-digit code with unlimited guesses is a four-digit code — and eight
failures freeze the account for 30 minutes.

## Sessions

12 hours, not the month a personal tool can afford. HMAC-SHA256 over compact
JSON rather than a JWT library: we only ever issue these to ourselves, and a
dependency is a liability. Signature compared in constant time, expiry checked
on read, signing key outside the database at 0600.

The client keeps the token in `sessionStorage`, not `localStorage`. Closing the
tab ends the session — mildly inconvenient, and the right trade on a shared or
family device.

## Transport

Set on every response by `install_security`:

* `Cache-Control: no-store` — a tax return has no business in a shared cache.
* `Content-Security-Policy` with `default-src 'self'`, no inline script, and
  `frame-ancestors 'none'`.
* `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`.
* HSTS with preload over HTTPS.

Rate limits are tightest on exactly the endpoints that send codes — an
unlimited one-time-code endpoint is an SMS bill and a brute-force oracle at the
same time.

## Isolation between accounts

Every data route resolves the account from the token, never from an id in the
request body — which is how horizontal privilege bugs happen. Another account's
record answers **404, not 403**: confirming that an id exists is itself a small
leak. One SSN cannot be claimed by two accounts, because two accounts with one
SSN cause rejected filings and is also what an account takeover looks like.

## The audit trail

IRS Publication 4557 requires one, and it is the only way to answer "who looked
at this client's SSN, and when". `AuditEvent` is append-only and records
sign-in, mobile verification, identity verification, SSN conflicts, document
upload, estimate runs, filing-history checks and payment choices — with the
last four digits, never the number.

## Deployment

| Variable | Purpose |
|---|---|
| `TAXVAULT_MASTER_KEY` | base64 32+ bytes, from KMS. **Set this in production.** |
| `TAXVAULT_SESSION_SECRET` | token signing key; rotating it invalidates all sessions |
| `TAXVAULT_DATABASE_URL` | PostgreSQL DSN; SQLite is for development only |
| `TAXVAULT_SMTP_*` | email delivery for sign-in codes |
| `TAXVAULT_SMS_*` | SMS gateway for mobile verification |
| `TAXVAULT_FORCE_HSTS` | send HSTS behind a TLS-terminating proxy |
| `TAXVAULT_DEV_CODES` | **development only** — returns codes in the API response |

`TAXVAULT_DEV_CODES` refuses to engage when a non-SQLite database is configured,
and `/api/auth/describe` and `/api/health` both report when it is on, so nobody
can forget.

## Known limits

Stated because a security document that only lists strengths is marketing:

* Identity verification is not KBA or document verification against a bureau.
* SMS delivery has no gateway wired in; `send_sms_code` raises rather than
  pretending.
* The rate limiter is in-process. Behind more than one worker it needs Redis.
* Uploaded documents are encrypted in the database rather than in object
  storage with its own lifecycle policy.
* There is no key-rotation job yet, only the per-ciphertext key id that makes
  one possible.
