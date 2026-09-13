"""The agentic loop.

Claude decides what to look at, in what order, and when it has enough to
answer. It does that by calling the CareerOS engines as tools.

This is a **manual** tool-use loop rather than the SDK's beta tool runner, for
three reasons that are specific to this product:

* every tool call is persisted to `agent_tool_call` as an audit trail, and the
  mutating ones need to be distinguishable after the fact;
* a turn cap and a mutation cap have to be enforced by the caller, not
  requested of the model; and
* this is a core product path, so it should not depend on a beta helper.

The loop owns control flow. It does not own truth: see `careeros.agent.guards`
for the capabilities that are withheld and the checks that run inside the tools
that remain.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from careeros.agent.tools import AgentContext, ToolSpec, build_tools, serialize_result
from careeros.ai.provider import DEFAULT_MODEL, get_provider
from careeros.db.models import AgentRun, AgentToolCall, User
from careeros.pipeline import Pipeline

logger = logging.getLogger(__name__)

DEFAULT_MAX_TURNS = 12
DEFAULT_MAX_TOKENS = 16000

SYSTEM_PROMPT = """You are CareerOS, a career agent working on behalf of one person.

You have tools over their job pipeline, their Master Profile, their Career \
Evidence Database, their applications, their job-related email and their \
outcome analytics. Use them. Do not answer career questions from general \
knowledge when a tool can tell you what is actually true for this person.

## How to work

Start by grounding yourself: `get_profile` tells you who this is, what work \
authorization they hold and which career tracks they are pursuing. Then go \
after the specific question.

Prefer evidence over inference. If you want to know whether they can do \
something, `search_evidence` and `get_match_detail` will tell you what they \
can actually demonstrate. If you want to know whether a strategy is working, \
`get_analytics` and `get_learning_signals` will tell you from their own \
outcome history.

Chain tools when a question needs it. A question like "why am I not getting \
interviews for cloud roles?" needs the analytics, the match detail on a few of \
those jobs, and the ATS report - not a guess.

Stop when you can answer. Do not call tools to look thorough.

## What you must never do

**Never claim experience the user cannot evidence.** The Career Evidence \
Database is the only permitted source for any resume or cover-letter claim. \
`get_match_detail` marks each requirement `claimable: true` or `false`. A \
`false` means they have adjacent experience and NOT the requirement itself. You \
may tell the user it is transferable. You may never present it to an employer \
as direct experience, and you may never suggest adding evidence to make a claim \
pass - you have no tool to do so, by design.

**Never overstate a work-authorization verdict.** Verdicts come from the \
country pack's rules applied to the posting's own words. `unknown` means the \
posting is silent, which is NOT the same as favourable. Quote the source phrase \
and repeat the disclaimer whenever you report one. You cannot change a verdict.

**Never present a hypothesis as a fact.** Rejection analysis separates what the \
employer actually said from what CareerOS guessed. Keep them separate when you \
report them.

**Never treat a small sample as a finding.** Analytics groups flagged \
`confident: false` do not support a conclusion. Say the sample is too small.

**Never describe a resume as ready when the factuality check failed.** \
`tailor_resume` returns `is_final: false` with the offending claims when it \
does. Report that plainly.

**Never send email.** There is no send tool. Drafts require the user's \
approval, and high-impact topics (immigration, sponsorship, salary, contracts, \
offers, resignation, legal) are held for them entirely.

## Tone

Be direct and specific. Name jobs by title and company, cite scores, quote the \
posting when it matters. Lead with the answer, then the reasoning. When the \
honest answer is "your evidence doesn't support that", say so - that is more \
useful than encouragement, because the screening call will find out anyway."""


class AgentUnavailable(RuntimeError):
    """Raised when the agent is asked to run with no model access.

    Unlike the scheduled pipeline, there is no heuristic substitute for an
    open-ended reasoning loop, and pretending otherwise would be worse than
    failing.
    """


@dataclass
class ToolCallRecord:
    turn: int
    name: str
    arguments: dict[str, Any]
    mutating: bool
    is_error: bool
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            "name": self.name,
            "arguments": self.arguments,
            "mutating": self.mutating,
            "is_error": self.is_error,
            "summary": self.summary,
        }


@dataclass
class AgentResult:
    answer: str
    turns: int = 0
    stop_reason: str | None = None
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    run_id: int | None = None
    ok: bool = True
    error: str | None = None

    @property
    def mutations(self) -> list[str]:
        return [c.name for c in self.tool_calls if c.mutating and not c.is_error]

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "turns": self.turns,
            "stop_reason": self.stop_reason,
            "tool_calls": [c.to_dict() for c in self.tool_calls],
            "mutations": self.mutations,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "run_id": self.run_id,
            "ok": self.ok,
            "error": self.error,
        }


class CareerAgent:
    def __init__(
        self,
        session: Session,
        user: User,
        client: Any | None = None,
        model: str | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
        effort: str = "high",
        use_ai_inside_tools: bool = True,
    ) -> None:
        self.session = session
        self.user = user
        provider = get_provider()
        self.client = client if client is not None else getattr(provider, "client", None)
        self.model = model or DEFAULT_MODEL
        self.max_turns = max_turns
        self.effort = effort
        self.context = AgentContext(
            session=session,
            user=user,
            pipeline=Pipeline(session),
            use_ai_inside_tools=use_ai_inside_tools,
        )
        self.tools: dict[str, ToolSpec] = build_tools(self.context)

    @property
    def available(self) -> bool:
        return self.client is not None

    def tool_definitions(self) -> list[dict[str, Any]]:
        # Sorted so the tool list is byte-identical across runs, which keeps
        # the cached prompt prefix valid (tools render before system).
        return [self.tools[name].definition() for name in sorted(self.tools)]

    # ------------------------------------------------------------------
    def _execute(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool, bool]:
        """Run one tool. Returns (serialised result, is_error, mutating)."""
        spec = self.tools.get(name)
        if spec is None:
            return (
                serialize_result(
                    {
                        "error": f"No such tool '{name}'",
                        "available_tools": sorted(self.tools),
                    }
                ),
                True,
                False,
            )
        try:
            value = spec.handler(**arguments)
        except TypeError as exc:
            return (
                serialize_result({"error": f"Bad arguments for {name}: {exc}"}),
                True,
                spec.mutating,
            )
        except Exception as exc:  # noqa: BLE001 - a tool fault must not kill the run
            logger.exception("tool %s failed", name)
            return (
                serialize_result({"error": f"{type(exc).__name__}: {exc}"}),
                True,
                spec.mutating,
            )
        is_error = isinstance(value, dict) and "error" in value
        return serialize_result(value), is_error, spec.mutating

    def _system_blocks(self) -> list[dict[str, Any]]:
        # One stable block, cached: the system prompt is identical every run, and
        # the loop re-sends it on every turn.
        return [
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    @staticmethod
    def _final_text(content: list[Any]) -> str:
        return "\n".join(
            block.text for block in content if getattr(block, "type", None) == "text"
        ).strip()

    # ------------------------------------------------------------------
    def run(self, prompt: str) -> AgentResult:
        """Drive the loop until Claude stops calling tools."""
        if not self.available:
            raise AgentUnavailable(
                "The agent needs model access. Set ANTHROPIC_API_KEY or run "
                "`ant auth login`. The scheduled pipeline (`careeros run-daily`) "
                "does not need it and still works."
            )

        run_row = AgentRun(user_id=self.user.id, prompt=prompt)
        self.session.add(run_row)
        self.session.flush()

        result = AgentResult(answer="", run_id=run_row.id)
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        tools = self.tool_definitions()

        try:
            for turn in range(1, self.max_turns + 1):
                result.turns = turn
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=DEFAULT_MAX_TOKENS,
                    system=self._system_blocks(),
                    messages=messages,
                    tools=tools,
                    thinking={"type": "adaptive"},
                    output_config={"effort": self.effort},
                )

                usage = getattr(response, "usage", None)
                if usage is not None:
                    result.input_tokens += getattr(usage, "input_tokens", 0) or 0
                    result.output_tokens += getattr(usage, "output_tokens", 0) or 0

                result.stop_reason = getattr(response, "stop_reason", None)

                if result.stop_reason == "refusal":
                    details = getattr(response, "stop_details", None)
                    result.ok = False
                    result.error = (
                        "The model declined this request"
                        + (f" (category: {getattr(details, 'category', None)})" if details else "")
                    )
                    result.answer = result.error
                    break

                # Echoed back unchanged, thinking blocks included -- required
                # when continuing a turn on the same model.
                messages.append({"role": "assistant", "content": response.content})

                tool_uses = [
                    b for b in response.content if getattr(b, "type", None) == "tool_use"
                ]
                if not tool_uses:
                    result.answer = self._final_text(response.content)
                    break

                tool_results: list[dict[str, Any]] = []
                for block in tool_uses:
                    arguments = dict(block.input or {})
                    payload, is_error, mutating = self._execute(block.name, arguments)
                    record = ToolCallRecord(
                        turn=turn,
                        name=block.name,
                        arguments=arguments,
                        mutating=mutating,
                        is_error=is_error,
                        summary=payload[:400],
                    )
                    result.tool_calls.append(record)
                    self.session.add(
                        AgentToolCall(
                            run_id=run_row.id,
                            turn=turn,
                            name=block.name,
                            arguments=arguments,
                            result_summary=payload[:2000],
                            mutating=mutating,
                            is_error=is_error,
                        )
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": payload,
                            **({"is_error": True} if is_error else {}),
                        }
                    )

                messages.append({"role": "user", "content": tool_results})
            else:
                # Loop exhausted without a final answer.
                result.ok = False
                result.error = f"Stopped after the {self.max_turns}-turn cap."
                result.answer = (
                    f"I ran out of turns ({self.max_turns}) before finishing. "
                    "Ask something narrower, or raise --max-turns."
                )
        except Exception as exc:  # noqa: BLE001 - surface, never crash the caller
            logger.exception("agent run failed")
            result.ok = False
            result.error = f"{type(exc).__name__}: {exc}"
            result.answer = f"The agent failed: {result.error}"

        if not result.answer and result.ok:
            result.answer = "(no answer produced)"

        run_row.answer = result.answer
        run_row.stop_reason = result.stop_reason
        run_row.turns = result.turns
        run_row.input_tokens = result.input_tokens
        run_row.output_tokens = result.output_tokens
        run_row.ok = result.ok
        run_row.error = result.error
        run_row.finished_at = datetime.now(timezone.utc)
        self.session.flush()
        return result
