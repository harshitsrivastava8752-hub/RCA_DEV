# path: agent/graph/graph.py
"""StateGraph wiring for the RCA investigation agent."""
from __future__ import annotations

from typing import Literal

from langgraph.graph import END, StateGraph

from agent.graph import nodes
from agent.graph.state import GraphState
from agent.rca import RCAOutput


def _route_after_update(state: GraphState) -> Literal["select_investigation", "conclude"]:
    if state.get("conclusion") is not None:
        return "conclude"
    if state["iteration"] >= state["max_iterations"]:
        return "conclude"
    top = nodes.leading_hypothesis(state["hypotheses"])
    if top is not None and top.status == "confirmed" and not top.contradicting:
        return "conclude"
    return "select_investigation"


def build_graph():
    graph = StateGraph(GraphState)
    graph.add_node("initial_observation", nodes.initial_observation)
    graph.add_node("generate_hypotheses", nodes.generate_hypotheses)
    graph.add_node("select_investigation", nodes.select_investigation)
    graph.add_node("execute_tools", nodes.execute_tools)
    graph.add_node("update_hypotheses", nodes.update_hypotheses)
    graph.add_node("conclude", nodes.conclude)

    graph.set_entry_point("initial_observation")
    graph.add_edge("initial_observation", "generate_hypotheses")
    graph.add_edge("generate_hypotheses", "select_investigation")
    graph.add_edge("select_investigation", "execute_tools")
    graph.add_edge("execute_tools", "update_hypotheses")
    graph.add_conditional_edges(
        "update_hypotheses",
        _route_after_update,
        {"select_investigation": "select_investigation", "conclude": "conclude"},
    )
    graph.add_edge("conclude", END)
    return graph.compile()


def run_investigation(incident_description: str, max_iterations: int = 12) -> RCAOutput:
    """Convenience entry point for callers (e.g. the M7 runner): builds the
    initial state, runs the compiled graph to completion, and returns the
    resulting RCAOutput. Not itself part of the §2/§3 contract."""
    app = build_graph()
    initial_state: GraphState = {
        "incident_description": incident_description,
        "observed_facts": [],
        "hypotheses": [],
        "evidence_log": [],
        "iteration": 0,
        "max_iterations": max_iterations,
        "conclusion": None,
        "next_action": None,
        "last_observation": None,
    }
    # Each loop pass is 3 nodes (select_investigation, execute_tools,
    # update_hypotheses); default recursion_limit=25 would cut off well
    # before max_iterations=12 is reached, so size it explicitly.
    final_state = app.invoke(initial_state, config={"recursion_limit": max_iterations * 6 + 20})
    conclusion = final_state.get("conclusion")
    if conclusion is None:
        conclusion = nodes.llm_unavailable_rca(final_state, "graph ended without a conclusion")
    return conclusion
