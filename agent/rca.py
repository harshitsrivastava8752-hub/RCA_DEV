# path: agent/rca.py
"""RCA output schema and Markdown rendering. Exact contract per SHARED_CONTEXT.md §3."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class TimelineEvent(BaseModel):
    timestamp: str
    description: str


class RCAOutput(BaseModel):
    root_cause: str
    confidence: Literal["low", "medium", "high"]
    evidence: list[str]
    timeline: list[TimelineEvent]
    alternative_explanations: list[str]
    uncertainty: str


def render_markdown(rca: RCAOutput) -> str:
    lines: list[str] = ["# Root Cause Analysis", "", "## Root Cause", rca.root_cause, ""]

    lines.append("## Evidence")
    if rca.evidence:
        lines.extend(f"- {item}" for item in rca.evidence)
    else:
        lines.append("- (none recorded)")
    lines.append("")

    lines.append("## Timeline")
    if rca.timeline:
        lines.extend(f"- `{event.timestamp}` — {event.description}" for event in rca.timeline)
    else:
        lines.append("- (none recorded)")
    lines.append("")

    lines += ["## Confidence", rca.confidence, ""]

    lines.append("## Alternative Explanations")
    if rca.alternative_explanations:
        lines.extend(f"- {item}" for item in rca.alternative_explanations)
    else:
        lines.append("- (none identified)")
    lines.append("")

    lines += ["## Uncertainty", rca.uncertainty, ""]
    return "\n".join(lines)
