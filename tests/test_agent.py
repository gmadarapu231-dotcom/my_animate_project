"""The agentic layer.

Two things are tested here and they matter differently:

* **Loop mechanics** -- parallel tool calls, chaining, bad tool names, bad
  arguments, the turn cap, refusals, the audit trail. Driven by a scripted fake
  client, so no network and no nondeterminism.
* **The safety model** -- that the withheld capabilities really are absent, and
  that the gates inside the surviving tools cannot be skipped. These are the
  tests that would catch someone "helpfully" adding an `add_evidence` tool.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from careeros.agent import AgentUnavailable, CareerAgent
from careeros.agent.guards import (
    FORBIDDEN_CAPABILITIES,
    ToolSurfaceViolation,
    assert_tool_surface_is_safe,
    enforce_draft_gate,
    enforce_resume_gate,
)
from careeros.agent.tools import AgentContext, build_tools
from careeros.ai.provider import get_provider
from careeros.db.models import AgentRun, AgentToolCall, EmailMessage, Job
from careeros.pipeline import Pipeline


# ---------------------------------------------------------------------------
# A scripted stand-in for the Anthropic client
# ---------------------------------------------------------------------------
def _block(**kwargs):
    return SimpleNamespace(**kwargs)


def _turn(*content, stop_reason="tool_use", input_tokens=100, output_tokens=20, **extra):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=list(content),
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        **extra,
    )


def _use(tool_id, name, **arguments):
    return _block(type="tool_use", id=tool_id, name=name, input=arguments)


def _text(value):
    return _block(type="text", text=value)


class FakeClient:
    """Replays a fixed list of responses and records the requests it received."""

    def __init__(self, script):
        self.script = list(script)
        self.requests: list[dict] = []
        outer = self

        class _Messages:
            def create(self, **kwargs):
                outer.requests.append(kwargs)
                index = len(outer.requests) - 1
                if index >= len(outer.script):
                    return _turn(_text("done"), stop_reason="end_turn")
                return outer.script[index]

        self.messages = _Messages()


@pytest.fixture
def agent_ctx(loaded):
    session = loaded["pipeline"].session
    return AgentContext(
        session=session,
        user=loaded["user"],
        pipeline=Pipeline(session, provider=get_provider("off")),
        use_ai_inside_tools=False,
    )


@pytest.fixture
def tools(agent_ctx):
    return build_tools(agent_ctx)


def make_agent(loaded, script, **kwargs):
    return CareerAgent(
        loaded["pipeline"].session,
        loaded["user"],
        client=FakeClient(script),
        use_ai_inside_tools=False,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# The safety model
# ---------------------------------------------------------------------------
def test_no_forbidden_capability_is_exposed(tools):
    """The core guarantee: these tools do not exist, so no prompt reaches them."""
    assert_tool_surface_is_safe(tools)
    for name in FORBIDDEN_CAPABILITIES:
        assert name not in tools, f"{name} must never be a tool"


def test_guard_catches_a_forbidden_tool_being_added():
    with pytest.raises(ToolSurfaceViolation, match="add_evidence"):
        assert_tool_surface_is_safe(["list_jobs", "add_evidence"])


def test_there_is_no_way_to_write_evidence(tools):
    """Evidence is the sole support for every claim, so it is read-only here."""
    for name in tools:
        assert "evidence" not in name or name == "search_evidence"


def test_no_send_or_submit_tool(tools):
    assert not any(
        word in name for name in tools for word in ("send", "submit", "approve")
    )


def test_every_tool_has_a_usable_schema(tools):
    for spec in tools.values():
        definition = spec.definition()
        assert definition["name"] and definition["description"]
        schema = definition["input_schema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        for required in schema.get("required", []):
            assert required in schema["properties"]


def test_resume_gate_forces_is_final_false_when_the_check_fails():
    class Report:
        passed = False
        summary = "1 unsupported"
        unsupported = [SimpleNamespace(text="Led Kubernetes migration", issues=["kubernetes"])]

    doc = SimpleNamespace(
        id=1, name="r", storage_path="/p", is_final=True, tailoring_notes=[]
    )
    payload = enforce_resume_gate(doc, Report())
    assert doc.is_final is False
    assert payload["is_final"] is False
    assert payload["unsupported_claims"]
    assert "CANNOT be sent" in payload["instruction"]
    assert "Never suggest adding evidence" in payload["instruction"]


def test_resume_gate_allows_a_clean_resume():
    class Report:
        passed = True
        summary = "all supported"
        unsupported: list = []

    doc = SimpleNamespace(id=1, name="r", storage_path="/p", is_final=False, tailoring_notes=[])
    payload = enforce_resume_gate(doc, Report())
    assert doc.is_final is True
    assert payload["factuality_passed"] is True
    assert "unsupported_claims" not in payload


def test_draft_gate_holds_high_impact_replies():
    draft = SimpleNamespace(
        intent="sponsorship_enquiry_response", subject="Re: x", body="…",
        state="blocked_high_impact", high_impact_topics=["immigration"],
    )
    payload = enforce_draft_gate(draft)
    assert payload["may_auto_send"] is False
    assert "cannot be approved or sent" in payload["instruction"]


def test_draft_gate_still_requires_approval_for_ordinary_replies():
    draft = SimpleNamespace(
        intent="interview_availability", subject="Re: x", body="…",
        state="awaiting_approval", high_impact_topics=[],
    )
    payload = enforce_draft_gate(draft)
    assert payload["may_auto_send"] is False
    assert "requires the user's approval" in payload["instruction"]


# ---------------------------------------------------------------------------
# Tool behaviour
# ---------------------------------------------------------------------------
def test_read_tools_return_real_pipeline_data(tools, loaded):
    jobs = tools["list_jobs"].handler()
    assert jobs["count"] > 0
    first = jobs["jobs"][0]
    assert first["deadline_bucket"] == "today"          # same ranking as the pipeline

    detail = tools["get_job"].handler(job_id=first["job_id"])
    assert detail["classification"]["domain"]
    assert detail["description_excerpt"]

    profile = tools["get_profile"].handler()
    assert profile["name"] == "Priya Raman"
    assert {a["country"] for a in profile["work_authorization"]} == {"US", "IN"}
    assert len(profile["career_tracks"]) == 4


def test_get_job_reports_the_verdict_with_its_source(tools, agent_ctx):
    job = agent_ctx.session.scalars(
        select(Job).where(Job.company == "Vertex Health")
    ).first()
    payload = tools["get_job"].handler(job_id=job.id)
    auth = payload["work_authorization"]
    assert auth["verdict"] == "not_compatible"
    assert auth["source_quotes"] and auth["source_quotes"][0]
    assert "verify with employer" in auth["disclaimer"]
    assert "cannot change it" in auth["note"]


def test_match_detail_marks_what_may_be_claimed(tools, agent_ctx):
    job = agent_ctx.session.scalars(
        select(Job).where(Job.company == "Halden Consulting")
    ).first()
    payload = tools["get_match_detail"].handler(job_id=job.id)
    assert payload["requirements"]
    assert any(r["claimable"] is False for r in payload["requirements"])
    assert "may never present it to an employer" in payload["note"]


def test_search_evidence_is_read_only_and_says_so(tools):
    payload = tools["search_evidence"].handler(skill="active_directory")
    assert payload["count"] >= 1
    assert "cannot add to it" in payload["note"]


def test_tailor_resume_tool_always_returns_a_factuality_verdict(tools, agent_ctx):
    job = agent_ctx.session.scalars(
        select(Job).where(Job.company == "Halden Consulting")
    ).first()
    payload = tools["tailor_resume"].handler(job_id=job.id)
    assert "factuality_passed" in payload
    assert payload["is_final"] == payload["factuality_passed"]
    assert any("unclaimed" in note.lower() for note in payload["notes"])


def test_draft_email_reply_tool_blocks_high_impact(tools, agent_ctx, tmp_path):
    from careeros.gmail.client import LocalMailboxClient
    from careeros.gmail.sync import GmailSync

    mailbox = tmp_path / "mail.json"
    mailbox.write_text(
        json.dumps({"messages": [{
            "id": "s1", "thread_id": "t1", "from": "hr@orrin.com",
            "subject": "Quick question", "received_at": "2026-09-13T08:00:00",
            "body": "Will you require H1B sponsorship now or in the future?",
        }]}),
        encoding="utf-8",
    )
    GmailSync(agent_ctx.session, LocalMailboxClient(mailbox)).sync(
        agent_ctx.user, generate_drafts=False, use_ai=False
    )
    agent_ctx.session.flush()
    message = agent_ctx.session.scalars(select(EmailMessage)).first()

    payload = tools["draft_email_reply"].handler(email_id=message.id)
    assert payload["state"] == "blocked_high_impact"
    assert "immigration" in payload["high_impact_topics"]
    assert payload["may_auto_send"] is False


def test_analytics_tool_flags_low_confidence(tools):
    payload = tools["get_analytics"].handler()
    assert "too few applications" in payload["note"]


def test_tools_handle_a_missing_id_without_raising(tools):
    assert "error" in tools["get_job"].handler(job_id=999_999)
    assert "error" in tools["get_match_detail"].handler(job_id=999_999)
    assert "error" in tools["tailor_resume"].handler(job_id=999_999)


def test_invalid_status_is_rejected_with_the_valid_set(tools, agent_ctx):
    from careeros.db.models import Application

    application = Application(user_id=agent_ctx.user.id, job_id=1)
    agent_ctx.session.add(application)
    agent_ctx.session.flush()
    payload = tools["update_application_status"].handler(
        application_id=application.id, status="teleported"
    )
    assert "error" in payload
    assert "applied" in payload["valid_statuses"]


# ---------------------------------------------------------------------------
# Loop mechanics
# ---------------------------------------------------------------------------
def test_agent_requires_model_access(loaded):
    agent = CareerAgent(
        loaded["pipeline"].session, loaded["user"], client=None, use_ai_inside_tools=False
    )
    assert agent.available is False
    with pytest.raises(AgentUnavailable, match="ANTHROPIC_API_KEY"):
        agent.run("anything")


def test_loop_chains_tools_and_returns_the_final_answer(loaded):
    agent = make_agent(loaded, [
        _turn(_use("a", "get_profile"), _use("b", "list_jobs", limit=3)),
        _turn(_use("c", "get_match_detail", job_id=1)),
        _turn(_text("Go after the Cobalt Bank role."), stop_reason="end_turn"),
    ])
    result = agent.run("What should I do today?")
    assert result.ok
    assert result.turns == 3
    assert result.answer == "Go after the Cobalt Bank role."
    assert [c.name for c in result.tool_calls] == [
        "get_profile", "list_jobs", "get_match_detail"
    ]
    assert result.input_tokens > 0 and result.output_tokens > 0


def test_parallel_tool_results_go_back_in_one_message(loaded):
    agent = make_agent(loaded, [
        _turn(_use("a", "get_profile"), _use("b", "list_jobs")),
        _turn(_text("ok"), stop_reason="end_turn"),
    ])
    agent.run("x")
    # request 2 carries the tool results: one user message holding both.
    second = agent.client.requests[1]["messages"]
    tool_result_messages = [
        m for m in second
        if m["role"] == "user" and isinstance(m["content"], list)
        and all(b.get("type") == "tool_result" for b in m["content"])
    ]
    assert len(tool_result_messages) == 1
    assert len(tool_result_messages[0]["content"]) == 2


def test_unknown_tool_is_reported_back_not_fatal(loaded):
    agent = make_agent(loaded, [
        _turn(_use("a", "add_evidence", text="I know SAP")),
        _turn(_text("I cannot add evidence."), stop_reason="end_turn"),
    ])
    result = agent.run("add SAP experience to my profile")
    assert result.ok
    call = result.tool_calls[0]
    assert call.is_error
    assert "No such tool" in call.summary
    assert result.mutations == []


def test_bad_arguments_are_reported_back(loaded):
    agent = make_agent(loaded, [
        _turn(_use("a", "get_job", nonexistent=1)),
        _turn(_text("recovered"), stop_reason="end_turn"),
    ])
    result = agent.run("x")
    assert result.tool_calls[0].is_error
    assert "Bad arguments" in result.tool_calls[0].summary


def test_turn_cap_is_enforced(loaded):
    # A script that never stops asking for tools.
    agent = make_agent(
        loaded, [_turn(_use(str(i), "get_profile")) for i in range(10)], max_turns=3
    )
    result = agent.run("loop forever")
    assert result.ok is False
    assert result.turns == 3
    assert "ran out of turns" in result.answer


def test_refusal_is_surfaced_not_swallowed(loaded):
    agent = make_agent(loaded, [
        _turn(
            _text(""),
            stop_reason="refusal",
            stop_details=SimpleNamespace(type="refusal", category="cyber"),
        )
    ])
    result = agent.run("do something disallowed")
    assert result.ok is False
    assert "declined" in result.answer
    assert "cyber" in result.answer


def test_mutating_calls_are_flagged_separately(loaded, agent_ctx):
    job = agent_ctx.session.scalars(
        select(Job).where(Job.company == "Halden Consulting")
    ).first()
    agent = make_agent(loaded, [
        _turn(_use("a", "list_jobs"), _use("b", "tailor_resume", job_id=job.id)),
        _turn(_text("done"), stop_reason="end_turn"),
    ])
    result = agent.run("tailor a resume for the SAP role")
    by_name = {c.name: c for c in result.tool_calls}
    assert by_name["list_jobs"].mutating is False
    assert by_name["tailor_resume"].mutating is True
    assert result.mutations == ["tailor_resume"]


def test_every_run_is_audited(loaded):
    session = loaded["pipeline"].session
    agent = make_agent(loaded, [
        _turn(_use("a", "get_profile")),
        _turn(_text("answer"), stop_reason="end_turn"),
    ])
    result = agent.run("who am I?")
    session.flush()

    run = session.get(AgentRun, result.run_id)
    assert run.ok and run.turns == 2
    assert run.prompt == "who am I?"
    assert run.answer == "answer"
    assert run.finished_at is not None

    calls = session.scalars(
        select(AgentToolCall).where(AgentToolCall.run_id == run.id)
    ).all()
    assert [c.name for c in calls] == ["get_profile"]
    assert calls[0].result_summary


def test_assistant_turns_are_echoed_back_verbatim(loaded):
    """Thinking blocks must survive the round trip on the same model."""
    thinking = _block(type="thinking", thinking="reasoning")
    agent = make_agent(loaded, [
        _turn(thinking, _use("a", "get_profile")),
        _turn(_text("ok"), stop_reason="end_turn"),
    ])
    agent.run("x")
    second = agent.client.requests[1]["messages"]
    assistant = next(m for m in second if m["role"] == "assistant")
    assert thinking in assistant["content"]


def test_tool_definitions_are_stable_across_runs(loaded):
    """Byte-identical tool lists keep the cached prompt prefix valid."""
    a = make_agent(loaded, []).tool_definitions()
    b = make_agent(loaded, []).tool_definitions()
    assert a == b
    assert [t["name"] for t in a] == sorted(t["name"] for t in a)


def test_system_prompt_is_cached(loaded):
    agent = make_agent(loaded, [_turn(_text("x"), stop_reason="end_turn")])
    agent.run("x")
    system = agent.client.requests[0]["system"]
    assert system[-1]["cache_control"] == {"type": "ephemeral"}


def test_loop_sends_adaptive_thinking_and_effort(loaded):
    agent = make_agent(loaded, [_turn(_text("x"), stop_reason="end_turn")], effort="medium")
    agent.run("x")
    request = agent.client.requests[0]
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"] == {"effort": "medium"}
    assert request["tools"]
