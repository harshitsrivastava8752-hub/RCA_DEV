# path: agent/graph/nodes.py
"""Node implementations for the RCA investigation graph."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import cast

from pydantic import BaseModel

from agent.graph.prompts import (
    CONCLUDE_SYSTEM,
    GENERATE_HYPOTHESES_SYSTEM,
    SELECT_INVESTIGATION_SYSTEM,
    UPDATE_HYPOTHESES_SYSTEM,
    build_conclude_user,
    build_generate_hypotheses_user,
    build_invalid_tool_notice,
    build_select_investigation_user,
    build_update_hypotheses_user,
)
from agent.graph.state import EvidenceEntry, GraphState, Hypothesis, NextAction
from agent.llm import LLMError, complete_json
from agent.rca import RCAOutput, TimelineEvent
from agent.tools.executor import TOOL_CATALOG, ToolResult, dispatch

# The demo app's only namespace (per SHARED_CONTEXT.md fixed names); the
# incident_description never names a different one in this prototype, so it
# is the sensible default for the deterministic baseline queries below.
DEFAULT_NAMESPACE = "demo-app"

ERROR_RATE_PROMQL = (
    'sum by (service) (rate(http_requests_total'
    '{service=~"frontend|checkout-api",status_code=~"5.."}[5m]))'
)


class _HypothesesPayload(BaseModel):
    hypotheses: list[Hypothesis]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _summarize_result(result: ToolResult) -> str:
    if not result.ok:
        text = f"ERROR: {result.error or 'unknown error'}"
    elif isinstance(result.data, str):
        text = result.data
    else:
        try:
            text = json.dumps(result.data, default=str)
        except TypeError:
            text = str(result.data)
    if len(text) > 300:
        text = text[:297] + "..."
    return text


def _fact_from_pods(result: ToolResult) -> str:
    if not result.ok:
        return f"Could not list pods in {DEFAULT_NAMESPACE}: {result.error}"
    count = len(result.data) if isinstance(result.data, list) else 0
    return f"{count} pod(s) observed in {DEFAULT_NAMESPACE}"


def _fact_from_events(result: ToolResult) -> str:
    if not result.ok:
        return f"Could not list events in {DEFAULT_NAMESPACE}: {result.error}"
    count = len(result.data) if isinstance(result.data, list) else 0
    return f"{count} recent event(s) in {DEFAULT_NAMESPACE} (last 30m)"


def _fact_from_promql(result: ToolResult) -> str:
    if not result.ok:
        return f"Baseline error-rate query failed: {result.error}"
    return "Baseline error-rate query executed against Prometheus"


def _sanitize_hypotheses(hypotheses: list[Hypothesis], fallback: list[Hypothesis]) -> list[Hypothesis]:
    """Guarantee unique, non-empty ids regardless of what the free-tier model
    returned, without otherwise altering the model's content."""
    if not hypotheses:
        return fallback
    seen: set[str] = set()
    cleaned: list[Hypothesis] = []
    for i, h in enumerate(hypotheses, start=1):
        hid = h.id if h.id and h.id not in seen else f"h{i}"
        while hid in seen:
            hid = f"{hid}x"
        seen.add(hid)
        cleaned.append(h if hid == h.id else h.model_copy(update={"id": hid}))
    return cleaned


def leading_hypothesis(hypotheses: list[Hypothesis]) -> Hypothesis | None:
    """The hypothesis the agent is currently treating as most likely: a
    confirmed one if any, else the active one with the most supporting
    evidence (also used by graph.py's routing to decide when to conclude)."""
    if not hypotheses:
        return None
    confirmed = [h for h in hypotheses if h.status == "confirmed"]
    if confirmed:
        return confirmed[0]
    active = [h for h in hypotheses if h.status == "active"]
    pool = active or hypotheses
    return max(pool, key=lambda h: len(h.supporting))


def llm_unavailable_rca(state: GraphState, detail: str) -> RCAOutput:
    evidence_log = state.get("evidence_log") or []
    hypotheses = state.get("hypotheses") or []
    return RCAOutput(
        root_cause="Unable to determine: the reasoning LLM became unavailable during the investigation.",
        confidence="low",
        evidence=[e.result_summary for e in evidence_log],
        timeline=[TimelineEvent(timestamp=e.timestamp, description=f"{e.tool_name}: {e.result_summary}") for e in evidence_log],
        alternative_explanations=[h.description for h in hypotheses],
        uncertainty=(
            f"Investigation halted early because the LLM backend failed ({detail}). "
            "The evidence and hypotheses above reflect only what had been gathered "
            "before the failure; no hypothesis was confirmed or refuted."
        ),
    )


def _fallback_action(leading: Hypothesis | None) -> NextAction:
    return NextAction(
        tool_name="k8s_get_events",
        tool_input={"namespace": DEFAULT_NAMESPACE, "since_minutes": 30},
        rationale="Fallback after two invalid tool selections; gathering recent events as a safe default.",
        target_hypothesis_id=leading.id if leading else None,
    )


def initial_observation(state: GraphState) -> dict:
    # Deterministic baseline gather, run once before any LLM reasoning. This
    # is the one exception to "only select_investigation/execute_tools touch
    # dispatch": the brief requires these three fixed calls up front, and
    # they are not LLM-selected, so there is no tool-selection loop to keep
    # dispatch calls confined to.
    namespace = DEFAULT_NAMESPACE
    pods_result = dispatch("k8s_get_pods", {"namespace": namespace})
    events_result = dispatch("k8s_get_events", {"namespace": namespace, "since_minutes": 30})
    promql_result = dispatch("prometheus_query", {"promql": ERROR_RATE_PROMQL, "lookback_minutes": 30})

    calls: list[tuple[str, dict, ToolResult]] = [
        ("k8s_get_pods", {"namespace": namespace}, pods_result),
        ("k8s_get_events", {"namespace": namespace, "since_minutes": 30}, events_result),
        ("prometheus_query", {"promql": ERROR_RATE_PROMQL, "lookback_minutes": 30}, promql_result),
    ]
    evidence = [
        EvidenceEntry(tool_name=name, tool_input=tool_input, result_summary=_summarize_result(res), timestamp=_now_iso())
        for name, tool_input, res in calls
    ]
    facts = [_fact_from_pods(pods_result), _fact_from_events(events_result), _fact_from_promql(promql_result)]
    return {
        "observed_facts": facts,
        "evidence_log": evidence,
        "last_observation": promql_result.wrapped_for_model,
    }


def generate_hypotheses(state: GraphState) -> dict:
    if state.get("conclusion") is not None:
        return {}
    user = build_generate_hypotheses_user(
        incident_description=state["incident_description"],
        observed_facts=state["observed_facts"],
        evidence_log=state["evidence_log"],
        last_observation=state.get("last_observation"),
    )
    try:
        result = cast(_HypothesesPayload, complete_json(GENERATE_HYPOTHESES_SYSTEM, user, _HypothesesPayload, max_retries=3))
    except LLMError as exc:
        return {"conclusion": llm_unavailable_rca(state, str(exc))}

    hypotheses = _sanitize_hypotheses(result.hypotheses, fallback=[])
    if not hypotheses:
        hypotheses = [
            Hypothesis(id="h1", description="Unknown cause — no hypotheses could be generated.", status="active", supporting=[], contradicting=[])
        ]
    return {"hypotheses": hypotheses}


def select_investigation(state: GraphState) -> dict:
    if state.get("conclusion") is not None:
        return {}
    leading = leading_hypothesis(state["hypotheses"])
    system = SELECT_INVESTIGATION_SYSTEM
    user = build_select_investigation_user(
        incident_description=state["incident_description"],
        hypotheses=state["hypotheses"],
        leading=leading,
        evidence_log=state["evidence_log"],
        last_observation=state.get("last_observation"),
        catalog=TOOL_CATALOG,
    )
    try:
        action = cast(NextAction, complete_json(system, user, NextAction, max_retries=3))
    except LLMError as exc:
        return {"conclusion": llm_unavailable_rca(state, str(exc))}

    if action.tool_name not in TOOL_CATALOG:
        retry_user = user + build_invalid_tool_notice(action.tool_name, TOOL_CATALOG)
        try:
            action = cast(NextAction, complete_json(system, retry_user, NextAction, max_retries=2))
        except LLMError as exc:
            return {"conclusion": llm_unavailable_rca(state, str(exc))}
        if action.tool_name not in TOOL_CATALOG:
            action = _fallback_action(leading)

    return {"next_action": action}


def execute_tools(state: GraphState) -> dict:
    if state.get("conclusion") is not None:
        return {}
    action = state["next_action"]
    result = dispatch(action.tool_name, action.tool_input)
    entry = EvidenceEntry(
        tool_name=action.tool_name,
        tool_input=action.tool_input,
        result_summary=_summarize_result(result),
        timestamp=_now_iso(),
    )
    return {
        "evidence_log": state["evidence_log"] + [entry],
        "last_observation": result.wrapped_for_model,
    }


def update_hypotheses(state: GraphState) -> dict:
    if state.get("conclusion") is not None:
        return {"iteration": state["iteration"]}
    user = build_update_hypotheses_user(
        incident_description=state["incident_description"],
        hypotheses=state["hypotheses"],
        evidence_log=state["evidence_log"],
        last_observation=state.get("last_observation"),
        next_action=state.get("next_action"),
    )
    try:
        result = cast(_HypothesesPayload, complete_json(UPDATE_HYPOTHESES_SYSTEM, user, _HypothesesPayload, max_retries=3))
    except LLMError as exc:
        return {
            "conclusion": llm_unavailable_rca(state, str(exc)),
            "iteration": state["iteration"] + 1,
        }

    hypotheses = _sanitize_hypotheses(result.hypotheses, fallback=state["hypotheses"])
    return {"hypotheses": hypotheses, "iteration": state["iteration"] + 1}


def conclude(state: GraphState) -> dict:
    if state.get("conclusion") is not None:
        return {}
    top = leading_hypothesis(state["hypotheses"])
    confirmed = bool(top and top.status == "confirmed" and not top.contradicting)
    user = build_conclude_user(
        incident_description=state["incident_description"],
        observed_facts=state["observed_facts"],
        hypotheses=state["hypotheses"],
        evidence_log=state["evidence_log"],
    )
    try:
        rca = cast(RCAOutput, complete_json(CONCLUDE_SYSTEM, user, RCAOutput, max_retries=3))
    except LLMError as exc:
        return {"conclusion": llm_unavailable_rca(state, str(exc))}

    # §3: on hitting max_iterations without a confirmed hypothesis, conclude
    # must still emit confidence="low" — enforce this regardless of what the
    # model said, rather than merely nudging it in the prompt.
    if not confirmed and rca.confidence != "low":
        rca = rca.model_copy(update={"confidence": "low"})
    return {"conclusion": rca}
