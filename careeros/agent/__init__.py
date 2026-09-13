"""The agentic layer: Claude drives the engines through a tool-use loop.

`careeros.pipeline` is the scheduled, deterministic path. This package is the
interactive one: the model decides *what to look at and in what order*, and
calls the same engines as tools.

The split is deliberate. The agent has autonomy over strategy and none over
truthfulness -- see `careeros.agent.guards`.
"""

from careeros.agent.loop import AgentResult, AgentUnavailable, CareerAgent

__all__ = ["CareerAgent", "AgentResult", "AgentUnavailable"]
