# path: agent/graph/state.py
"""InvestigationState and companion schemas. Exact contract per SHARED_CONTEXT.md §2."""
from __future__ import annotations

from typing import Literal, TypedDict

from pydantic import BaseModel

from agent.rca import RCAOutput


class Hypothesis(BaseModel):
    id: str
    description: str
    status: Literal["active", "confirmed", "refuted"]
    supporting: list[str]
    contradicting: list[str]


class EvidenceEntry(BaseModel):
    tool_name: str
    tool_input: dict
    result_summary: str
    timestamp: str


class NextAction(BaseModel):
    tool_name: str
    tool_input: dict
    rationale: str
    target_hypothesis_id: str | None = None


class InvestigationState(TypedDict):
    incident_description: str
    observed_facts: list[str]
    hypotheses: list[Hypothesis]
    evidence_log: list[EvidenceEntry]
    iteration: int
    max_iterations: int
    conclusion: RCAOutput | None


class GraphState(InvestigationState, total=False):
    """Runtime state actually threaded through the compiled LangGraph: the
    exact §2 InvestigationState fields (all required, inherited above) plus
    internal-only plumbing that never appears in the public §2/§3 contract
    and that no other module should read or write.

    next_action: the NextAction chosen by select_investigation, consumed by
      execute_tools on the same pass.
    last_observation: the wrapped_for_model text of the most recently
      executed tool call, so the next LLM call gets full detail on it while
      older calls are only represented via evidence_log's result_summary
      (see §2 context-budget note).
    """

    next_action: NextAction | None
    last_observation: str | None
