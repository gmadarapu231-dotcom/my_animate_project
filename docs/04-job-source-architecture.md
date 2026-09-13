# 4. Job-Source Architecture

## Interface

Every connector implements one method:

```python
class JobSource(ABC):
    id: str                      # stored on each job row
    countries: tuple[str, ...]   # empty = any

    @abstractmethod
    def fetch(self, limit: int | None = None, **kwargs) -> Iterator[RawJob]: ...
    def healthcheck(self) -> tuple[bool, str]: ...
```

`RawJob` is the posting as the source saw it. Normalisation (country
resolution, salary parsing, employment-type detection) happens once, in the
pipeline, so no connector has to know how India formats a salary.

Adding LinkedIn or Naukri is a class plus a registry entry. Nothing downstream
changes — the classifier, eligibility engine and ranker never learn where a job
came from beyond `job.source`, which exists for analytics ("which board
actually converts?").

## Country packs declare the boards

```yaml
# config/countries/usa.yaml
job_boards:
  - {id: linkedin,   label: LinkedIn,   connector: linkedin,        enabled: false}
  - {id: dice,       label: Dice,       connector: dice,            enabled: false}
  - {id: greenhouse, label: Greenhouse, connector: greenhouse_board, enabled: false}
```

```yaml
# config/countries/india.yaml
job_boards:
  - {id: naukri,     label: Naukri,     connector: naukri,     enabled: false}
  - {id: instahyre,  label: Instahyre,  connector: instahyre,  enabled: false}
```

A country's board list is part of its pack, so adding a country brings its job
market with it.

## Deduplication

Boards cross-post. The same role appears on LinkedIn, Dice and the company's
own Greenhouse page with three different ids.

`RawJob.fingerprint()` builds a cross-source identity:

1. Normalise the title; expand `sr`/`snr` → `senior`, `jr` → `junior`.
2. Strip punctuation, drop words ≤ 2 characters, **sort the remaining words**
   into a set — so "Senior Network Engineer" and "Network Engineer, Senior"
   collide.
3. Combine with the alphanumeric-only company name and city.
4. SHA-256, first 32 hex characters.

On a fingerprint hit the pipeline **merges rather than skips**: a later source
may carry a deadline, an applicant count, a salary or a URL the first one
lacked, and the richest copy wins. Tested in
`test_cross_source_dedupe_by_fingerprint` and `test_ingest_deduplicates`.

Cities are part of the key on purpose. "Network Engineer @ Acme, Austin" and
"Network Engineer @ Acme, Dallas" are two jobs, and collapsing them would hide
one.

## Country resolution

Explicit `country` on the record wins. Otherwise `CountryRegistry.detect()`
scans the location string and the first 400 characters of the description for a
country name or alias, preferring the longest match. Two-letter ISO codes are
excluded from free-text matching — India's `in` would otherwise match every
occurrence of the English preposition.

## Phase 1: the file connector

`JsonFileSource` reads a JSON file or a directory of them. It is the working
connector today and doubles as the import path for postings exported from a
board or pasted in by hand.

It accepts relative dates — `posted_days_ago`, `deadline_in_days` — so fixtures
and demo data stay meaningful whenever they run. A fixture with a hard-coded
deadline silently becomes an "expired" job tomorrow, which quietly breaks the
one behaviour the product most wants to demonstrate.

```json
{"jobs": [{
  "id": "ln-88213", "source": "linkedin",
  "title": "Senior Network Security Engineer", "company": "Cobalt Bank",
  "country": "US", "city": "Austin", "region": "TX",
  "employment_type": "full_time", "salary": "$140,000 - $165,000 per year",
  "posted_days_ago": 2, "deadline_in_days": 0, "applicant_count": 31,
  "url": "https://example.com/jobs/ln-88213",
  "description": "…"
}]}
```

## Phase 2 connectors

| Connector | Access | Notes |
|---|---|---|
| Greenhouse / Lever / Ashby | Public board JSON APIs | Per-company, stable, no auth. Start here. |
| Indeed / LinkedIn / Dice | Partner or affiliate APIs | Terms-bound; requires an agreement, not a scraper |
| Naukri / Instahyre / Cutshort | Partner APIs | India-first |
| Email alerts | Gmail | Job-alert emails are already flowing into the mailbox — parse them into `RawJob`s |
| Manual paste | UI | A URL or pasted description; the highest-signal source a user has |

**Rules for every connector.** Respect `robots.txt` and the site's terms. Use
an official API where one exists. Rate-limit and back off. Never bypass
CAPTCHA, MFA or bot protection to *read* a board, exactly as the application
layer never bypasses them to *submit* (doc 9). If a board's terms forbid
automated access, it does not get a connector — the email-alert path exists
precisely because it is the user's own mail, arriving with permission.

## Freshness and expiry

`posted_on` feeds urgency when there is no stated deadline: a posting seen on
day 1 is worth more than the same posting on day 40, because the early
applicants are the ones a recruiter actually reads.

Jobs past their deadline are marked `archived=True` by the priority stage and
drop out of the default list, remaining available under `--all` / `?include_archived=true`
for the record.
