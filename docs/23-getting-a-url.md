# Getting a URL

There is no hosted TaxVault. Nobody can hand you a link to it, because the
application holds clients' Social Security numbers and the only sensible
place for that is a machine your firm controls. This page is how you get
from the source tree to an address a client can open.

Three steps, and you can stop after the first two for a long time.

---

## 1. On your own machine, now

```bash
pip install -e .
taxvault serve
```

`http://127.0.0.1:8000` — the whole application: upload a W-2, get an
estimate, download the document. Development codes are on, so the sign-in
code is shown on screen instead of emailed.

To use it from your phone on the same wifi, bind to the network instead:

```bash
taxvault serve --host 0.0.0.0
```

then open `http://<your-laptop's-ip>:8000` from the phone. **This is wifi
only.** There is no TLS, so it is fine for trying the app out and not fine
for a real client's W-2.

---

## 2. The app on a phone

There is no app store listing and there does not need to be one. The client
is a Progressive Web App: open the URL in Safari or Chrome and choose **Add
to Home Screen**. It gets an icon, opens without browser chrome, and behaves
like an installed app.

What that buys you, beyond not paying Apple: there is no review queue
between a fix and your clients having it. When you deploy, everyone is on
the new version at their next open. For a product whose correctness depends
on this year's rate tables, that matters more than a listing does.

It will not work offline in any useful way — the figures come from the
server — and that is deliberate. A tax estimate computed against a stale
cached rate table is worse than no estimate.

---

## 3. A real address, over HTTPS

You need a machine and a name. Any small VPS will do; the application is not
demanding, and the database is the only part that grows.

**Point the name at the machine first.** Caddy proves it controls the domain
over port 80, and a name that does not resolve yet fails that proof.

```
A    tax.yourfirm.com    →    <the machine's IP>
```

Then, on the machine:

```bash
git clone <your repository> && cd taxvault
cp .env.example .env
```

Fill in `.env`. The three that have no safe default:

```bash
taxvault newkey                                        # TAXVAULT_MASTER_KEY
python -c "import secrets; print(secrets.token_urlsafe(48))"   # SESSION_SECRET
# and a POSTGRES_PASSWORD you did not reuse from anywhere
```

Set `TAXVAULT_DOMAIN` and `TAXVAULT_ADMIN_EMAIL`, then:

```bash
docker compose -f docker-compose.yml -f docker-compose.public.yml up -d --build
```

That is the URL. Caddy gets a certificate from Let's Encrypt on first start
and renews it on its own; there is no certbot cron to forget. The
application itself is no longer reachable on a port of its own — everything
arrives over HTTPS or not at all.

Check it came up the way you meant:

```bash
docker compose exec app taxvault check
curl https://tax.yourfirm.com/api/health
```

`taxvault check` is the one that matters. In production the process
**refuses to start** without a real master key, a real session secret, and
development codes switched off. If it will not boot, that command says
which one.

---

## What must be true before a real client uses that URL

The URL is the easy part. These are not optional, and none of them is code:

* **The master key lives in a secret manager, not in `.env` on the box.**
  Everything sealed under it — every SSN, every bank detail — is
  unreadable without it, and it cannot be rotated casually. A machine that
  is lost with the key on it is a breach; a machine lost without it is an
  inconvenience.
* **The database is backed up, and you have restored one.** A backup you
  have never restored is a belief, not a backup.
* **Email and SMS work.** Without email nobody can sign in. Without SMS
  nobody can finish identity verification, because that gate requires a
  verified mobile. Send yourself both before the first client.
* **`TAXVAULT_DEV_CODES` is unset.** With it on, the sign-in code is in the
  response body. The boot gate refuses to start production with it set, and
  that gate is the only thing standing between you and an open front door.
* **You can still not file.** Transmitting a return needs an EFIN from IRS
  e-Services (allow 45 days), acceptance testing against Modernized e-File,
  and a signed Form 8879 per client. The application stops at
  ready-to-sign and a test asserts no code path goes further. See
  `22-tax-production.md`.
* **Your practice details are filled in** — name, address, phone, preparer,
  PTIN. They print on every estimate a client keeps and on their receipt.

---

## Which address to give whom

| | where | for |
|---|---|---|
| `127.0.0.1:8000` | your laptop | building, trying things |
| `<laptop ip>:8000` | your wifi | showing someone across the desk |
| `https://tax.yourfirm.com` | your server | clients |

Do not skip the middle one. Most of what goes wrong on a real deployment —
a form that will not read, a state that computes oddly — shows up just as
clearly on a laptop, where you can see the traceback.
