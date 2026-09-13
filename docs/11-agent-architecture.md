# 11. Agent Architecture

## Two execution models, on purpose

CareerOS has two ways to run, and the difference is who owns control flow.

| | `careeros.pipeline` | `careeros.agent` |
|---|---|---|
| Control flow | Fixed stages, in order, written in Python | Claude decides, turn by turn |
| Model role | Called at specific points for extraction and rewording | Drives everything, choosing tools |
| Runs without a key | **Yes** — every stage has a deterministic path | No |
| Good for | The scheduled morning run, reliability, bulk | Open-ended questions, investigation, judgement |
| Entry point | `careeros run-daily` | `careeros agent "…"` |

The pipeline is not a lesser version of the agent. It is the reliable path: it
processes every job the same way every morning, it cannot loop, it cannot
change its mind, and it works when the network is down. The agent is for the
questions a fixed pipeline cannot answer — *"why am I not getting interviews
for cloud roles?"* needs someone to look at the analytics, then at the match
detail on several of those jobs, then at the ATS report, and decide what the
pattern is.

They share every engine. If the agent and the pipeline disagree about a match
score, that is a bug, because both call `SkillMatcher.match`.

## The loop

`careeros/agent/loop.py`. A **manual** tool-use loop rather than the SDK's beta
tool runner, for three product-specific reasons:

1. Every tool call is persisted to `agent_tool_call` as an audit trail, and the
   mutating ones must be distinguishable after the fact.
2. The turn cap and the error-interception behaviour are enforced by the
   caller, not requested of the model.
3. This is a core product path; it should not depend on a beta helper.

```
user prompt
   │
   ▼
┌──────────────────────────────────────────────┐
│ messages.create(model, system, messages,     │  ← system prompt cached,
│                 tools, thinking=adaptive,    │    tool list byte-stable
│                 output_config={effort})      │
└────────────────┬─────────────────────────────┘
                 │
     stop_reason == "refusal"  ──► surface it, stop
     no tool_use blocks        ──► final answer, stop
                 │
                 ▼
   append response.content verbatim (thinking blocks included)
                 │
                 ▼
   execute every tool_use block, collect ALL results
   into ONE user message  ──► loop (max 12 turns)
```

Details that matter:

- **`response.content` is echoed back unchanged**, so thinking blocks survive
  the round trip — required when continuing a turn on the same model. Tested in
  `test_assistant_turns_are_echoed_back_verbatim`.
- **Parallel tool calls return in a single user message.** Splitting them
  teaches the model to stop calling tools in parallel. Tested in
  `test_parallel_tool_results_go_back_in_one_message`.
- **Adaptive thinking** with `output_config.effort` (default `high`).
- **The system prompt carries a `cache_control` breakpoint** and the tool list
  is sorted, so the cached prefix stays valid across turns and across runs —
  the loop re-sends both on every turn.
- **A tool fault is data, not an exception.** A bad tool name, bad arguments or
  a raised exception all come back as a `tool_result` with `is_error: true`, and
  the model recovers. Tested in `test_unknown_tool_is_reported_back_not_fatal`.
- **Turn cap** (default 12) ends the loop with an honest message rather than
  running forever.
- **Refusals are surfaced**, with the category, never swallowed.

## The tool surface

18 tools, all thin wrappers over existing engines (`careeros/agent/tools.py`).

**Read (12)** — `list_jobs`, `get_job`, `search_jobs`, `get_profile`,
`search_evidence`, `get_match_detail`, `get_ats_report`, `list_applications`,
`get_application`, `get_analytics`, `get_learning_signals`,
`list_email_messages`.

**Mutating (6)** — `analyze_job`, `tailor_resume`, `draft_cover_letter`,
`draft_email_reply`, `update_application_status`, `add_application_note`.

Tool outputs are compact by design: results re-enter the context window on the
next turn, so tools return summaries with ids to drill into rather than object
dumps (`get_job` returns a 1200-character description excerpt, not the whole
posting), and `serialize_result` caps any single result at 12 KB with a note to
narrow the query.

Several tools carry a `note` field that travels with the data — `get_job`'s
work-authorization block says *"you cannot change it"*, `get_match_detail` says
*"you may never present it to an employer as direct experience"*,
`get_analytics` says *"groups flagged `confident: false` do not support a
conclusion"*. Putting the constraint next to the data is more reliable than
stating it once in the system prompt, 8,000 tokens earlier.

## The safety model

**The agent has autonomy over strategy and none over truthfulness.**

### Mechanism 1: absent capabilities

The strongest guarantee is a tool that does not exist. `careeros/agent/guards.py`
enumerates the withheld capabilities *with the reason for each*, and
`assert_tool_surface_is_safe()` runs on every registry build — not once at
import — so a tool added later cannot slip one in.

| Withheld | Why |
|---|---|
| `add_evidence`, `verify_evidence`, `edit_evidence`, `delete_evidence` | Evidence is the user's verified truth and the sole support for every resume claim. **An agent that can write evidence can manufacture the support for anything it wants to say.** This is the most important line in the file. |
| `set_eligibility`, `override_verdict` | Verdicts come from the country pack's rules applied to the posting's own words. Not a model output. |
| `mark_resume_final`, `set_factuality_passed` | `is_final` is set by the factuality checker and nothing else. It is the gate, so it cannot be a tool. |
| `send_email`, `approve_draft` | Phase 1 never auto-sends; approval is the human's act. |
| `submit_application` | ASSISTED mode: CareerOS assembles, the human submits. |
| `execute_sql`, `run_shell` | Would route around everything above. |

Three tests assert this from different angles: no forbidden name is registered,
no tool name contains `send`/`submit`/`approve`, and the only evidence tool is
`search_evidence`. The API publishes the withheld list at `GET /api/agent/tools`
so it is inspectable rather than a claim in a README.

### Mechanism 2: gates inside the tools that remain

The check is part of the action, so the action cannot be performed without it:

- `tailor_resume` runs the factuality checker on every invocation and returns
  its verdict. `is_final` is assigned *from* that verdict by
  `enforce_resume_gate`. A failing check returns `is_final: false`, the
  offending claims, and an instruction not to describe the resume as ready —
  and explicitly not to suggest adding evidence to make it pass.
- `draft_email_reply` runs high-impact detection on both the incoming message
  and the generated reply. Any hit means `blocked_high_impact`, and there is no
  tool to approve or send it.

### Mechanism 3: the audit trail

`agent_run` and `agent_tool_call` record the prompt, the answer, the turn count,
token usage, and every tool call with its arguments, whether it mutated
anything, and whether it errored. Inspect it with `careeros agent-runs` or
`GET /api/agent/runs`. Mutating calls are flagged separately in the CLI output
(`*`) and in the dashboard, so "what did it actually change?" is answerable.

## Honest degradation

The agent raises `AgentUnavailable` without model access, and the CLI and API
say so plainly (`503`, pointing at `ANTHROPIC_API_KEY` / `ant auth login`, and
noting the pipeline still works). There is no heuristic substitute for an
open-ended reasoning loop, and faking one would be worse than failing.

`--no-ai-in-tools` is a middle setting: the loop uses the model, but the tools
use their deterministic paths. Cheaper, and useful when the question is about
strategy rather than wording.

## Cost

One request per turn. A typical question is 2–4 turns; the cap is 12. The
system prompt and tool list are cached, so the per-turn marginal cost is the
conversation and the tool results — which is why tool outputs are capped and
summarised rather than dumped.

## Interfaces

```bash
careeros agent "Why am I not getting interviews for cloud roles?"
careeros agent "What should I apply for today, and why that order?" --show-tools
careeros agent "Can I honestly apply for the SAP role?" --json
careeros agent-runs --limit 5
```

```
POST /api/agent/ask    {prompt, max_turns, effort, use_ai_in_tools}
GET  /api/agent/tools  the surface + the withheld capabilities and why
GET  /api/agent/runs   audit trail
```

The dashboard's **Agent** tab shows the answer, the tool calls that produced it
(marked read / changed / error), token usage, and a link to the tool surface —
because an agent whose reasoning you cannot inspect is one you cannot correct.
