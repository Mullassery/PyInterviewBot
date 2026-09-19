"""The competency/evidence graph (spec §21) for this slice's one demo
competency: RAG Architecture. A production system would load this from a
job-description-driven competency library; here it's a fixed definition
matching the §36 demo scenario exactly.
"""

from __future__ import annotations

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
    nodes: dict[str, NodeState] = field(default_factory=lambda: {n: NodeState() for n in NODES})

    def apply(self, node: str, status: EvidenceStatus, note: str) -> None:
        if node not in self.nodes:
            # The LLM invented a node id we don't track — ignore rather than
            # crash the turn; the raw note is still visible in the transcript.
            return
        self.nodes[node] = NodeState(status=status, note=note)

    def sufficiently_covered(self) -> bool:
        addressed = [n for n in self.nodes.values() if n.status != "not_addressed"]
        return len(addressed) == len(self.nodes)

    def summary_for_prompt(self) -> str:
        lines = []
        for node_id, description in NODES.items():
            state = self.nodes[node_id]
            lines.append(f"- {node_id} ({description}): {state.status}" + (f" — {state.note}" if state.note else ""))
        return "\n".join(lines)
