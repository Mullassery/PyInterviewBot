"""Evidence graph state for the active interview template.

The default RAG nodes remain exported for backwards compatibility. New
templates pass their own node ids/descriptions so evidence stays grounded in
the job-relevant rubric rather than a hardcoded demo.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .models import EvidenceStatus

COMPETENCY = "rag_architecture"

NODES: dict[str, str] = {
    "retrieval_design": "How retrieval is designed — what's fetched vs. handled directly by the model",
    "relevance_handling": "What happens when retrieval returns irrelevant or low-quality documents",
    "production_scaling": "How the system behaves and is investigated under a large traffic/latency increase",
}


@dataclass
class NodeState:
    status: EvidenceStatus = "not_addressed"
    note: str = ""


@dataclass
class EvidenceGraphState:
    node_specs: Mapping[str, str] = field(default_factory=lambda: dict(NODES))
    nodes: dict[str, NodeState] = field(init=False)

    def __post_init__(self) -> None:
        self.nodes = {node_id: NodeState() for node_id in self.node_specs}

    def apply(self, node: str, status: EvidenceStatus, note: str) -> None:
        if node not in self.nodes:
            return
        self.nodes[node] = NodeState(status=status, note=note)

    def sufficiently_covered(self) -> bool:
        addressed = [n for n in self.nodes.values() if n.status != "not_addressed"]
        return len(addressed) == len(self.nodes)

    def statuses(self) -> dict[str, str]:
        return {node_id: state.status for node_id, state in self.nodes.items()}

    def summary_for_prompt(self) -> str:
        lines = []
        for node_id, description in self.node_specs.items():
            state = self.nodes[node_id]
            lines.append(
                f"- {node_id} ({description}): {state.status}"
                + (f" — {state.note}" if state.note else "")
            )
        return "\n".join(lines)
