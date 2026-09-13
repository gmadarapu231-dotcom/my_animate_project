# 3. AI Architecture

## Principle: the model sharpens, it does not gate

Every capability has a deterministic implementation that always runs, and an
optional LLM pass that refines it. Nothing in the product stops working without
an API key — you lose nuance, not function. This is why the whole test suite
(104 tests) runs with `CAREEROS_LLM=off` and still exercises classification,
matching, ATS, prioritisation, tailoring, factuality, email and search.

```
 input ──► deterministic engine ──► result
                                      │
                       (if a key is configured)
                                      ▼
              LLM structured call ──► merge ──► result'
                                      │
                          (on failure/refusal: result stands)
```

## Provider layer

`careeros/ai/provider.py` exposes two primitives behind a `Protocol`:

| Method | Use | Shape |
|---|---|---|
| `structured(system, prompt, schema, cacheable_context, effort)` | Classification, extraction, tailoring, search compilation | `client.messages.parse(...)` with a Pydantic `output_format`; returns a validated object or `None` |
| `text(system, prompt, max_tokens, effort)` | Cover letters, email replies | `client.beta.messages.create(...)`; returns a string or `None` |

Implementations: `AnthropicProvider` (default when credentials resolve) and
`HeuristicProvider` (returns `None` from everything). `get_provider("off")` pins
the heuristic path for tests.

### Model and call configuration

- Model: **`claude-opus-5`** (override with `CAREEROS_LLM_MODEL`).
- **Adaptive thinking** (`thinking={"type": "adaptive"}`) on the text path;
  depth is steered with `output_config.effort`, not a token budget.
- **Server-side refusal fallbacks** enabled on the text path
  (`betas=["server-side-fallback-2026-07-01"]`, `fallbacks="default"`), so a
  declined request is rerouted rather than failing the morning run.
- `stop_reason == "refusal"` is checked **before** reading content on both
  paths; a refusal returns `None` and the deterministic result stands.
- **Prompt caching**: the stable half of each prompt (the domain taxonomy, the
  user's evidence set) is passed as `cacheable_context` and marked with
  `cache_control: {"type": "ephemeral"}`, placed last in the `system` array. The
  volatile job text lives in `messages`, after the cached prefix. The daily run
  classifies many jobs against the same taxonomy, so this is the difference
  between paying for the taxonomy once and paying for it per job.
- Credentials: a bare `Anthropic()` constructor is used when no explicit key is
  present, so an `ant auth login` profile is picked up. An unset
  `ANTHROPIC_API_KEY` does not by itself mean "no credentials".

Every call is wrapped: any exception is logged at WARNING and returns `None`.
The pipeline cannot be taken down by the AI layer.

## Where the model is used, and what it may decide

| Task | Deterministic layer | What the model adds | May it override? |
|---|---|---|---|
| Job classification | Weighted taxonomy scoring | **Inventing a domain the taxonomy lacks**; better function/specialisation/industry; skills in the JD's own words | Yes, when more confident, or when the heuristic said `other` |
| ATS keywords | Per-posting n-gram scoring | Industry-appropriate phrasing | Merged, model's first |
| Resume tailoring | Evidence selection + ordering | Rewording bullets, assembling the summary | Only text, and only for blocks citing exactly one evidence item |
| Factuality check | Entity re-derivation | — | **No. The gate is deterministic on purpose.** |
| Email extraction | Signature matching + regex | Company/position/dates, stated rejection reason | Yes, except the rejection quote (verified as a real substring first) |
| Email replies | Templates | Natural phrasing | Yes; the high-impact check then re-runs on the output |
| NL search | Rule-based compiler | Unrecognised phrasings | Union only — the model may add filters, not drop them |

Two deliberate exclusions:

- **The factuality checker never calls the model.** Its job is to catch the
  model inventing something; asking the model to audit itself defeats it.
- **The model is never asked for an immigration conclusion.** It reads phrases;
  the country pack's rules produce the verdict.

## Structured output contracts

`careeros/ai/schemas.py` — `JobClassificationOut`, `TailoringOut`,
`FactualityOut`, `EmailExtractionOut`, `SearchFilterOut`. These double as the
interchange format between the deterministic and AI layers, so callers never
branch on which one ran.

## Prompt rules that carry the product's guarantees

**Classification** — *"If none of the seeded domains fits, INVENT a new
snake_case `domain_id`. Never force a job into a domain that does not describe
it. `ats_keywords` are the terms an ATS would filter on for THIS posting and
THIS industry — derive them from the text, do not reuse a generic technology
list. Do not infer work authorization, visa status or salary here."*

**Tailoring** — *"Every bullet MUST cite the `evidence_id` it was derived from.
You MUST NOT introduce any employer, project, technology, tool, metric,
certification, degree, job title, responsibility, achievement, security
clearance or visa status that is not present in the evidence item you cite. You
MUST NOT change a number, a date, a scale or an outcome. If a job requirement is
not supported by the evidence, say nothing about it. Do not hint, do not imply
adjacency. Leave the gap."*

**Email extraction** — *"`rejection_reason_explicit` is filled ONLY when the
sender states a reason. A polite form rejection has no stated reason.
`rejection_reason_quote` must be an exact substring of the email."*

**Email replies** — *"Never state, confirm or negotiate a visa status,
sponsorship arrangement, salary figure, contract term or offer decision."*

Prompt rules are necessary but not sufficient, which is why each has a
deterministic check behind it: the factuality gate for tailoring, substring
verification for rejection quotes, `detect_high_impact` for replies.

## The skill graph

`config/taxonomy/skills.yaml` — 95 nodes with `aliases`, `related`, `broader`,
`narrower`, weighted `direct 1.0 / narrower 0.8 / broader 0.55 / related 0.45`.
Edges are declared one way and closed symmetrically at load. `SkillScanner`
compiles every alias into chunked alternations with alias-boundary handling for
`c#`, `ci/cd`, `.net`, `s/4hana`, and tolerates regular plurals.

Skills the classifier meets but cannot place go to `learned_skill` with an
observation count. Promotion into the curated YAML is a human review step — the
shipped graph stays curated while the system still grows.

## Cost and latency

- Classification: one call per *new* job (never re-classified unless asked).
- Tailoring: one call per job in the daily top-N (default 5).
- Email: one extraction + one draft per *new* action-requiring message.
- Search: one call per natural-language query.

A typical morning on 20 new postings is ~5 classification calls after dedupe,
5 tailoring calls and a handful of email calls, with the taxonomy and evidence
prefix cached across them.
