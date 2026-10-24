"""Versioned interview templates: competencies, evidence nodes, and prompts.

Templates are the interview engine's configuration boundary. Voice and model
providers never own sequencing or completion rules; they receive a bounded
context built from these definitions.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .providers.base import EvidenceNodeSpec


@dataclass(frozen=True)
class InterviewTemplate:
    id: str
    title: str
    role: str
    competency: str
    description: str
    nodes: tuple[EvidenceNodeSpec, ...]
    question_bank: tuple[str, ...]
    max_turns: int = 6
    system_prompt: str = ""
    evaluation_notes: str = ""


DEFAULT_TEMPLATE_ID = "rag_architect"

RAG_SYSTEM_PROMPT = """You are a senior technical interviewer conducting a live spoken interview.
Your tone is professional, calm, intelligent, neutral, and conversational — never a checklist,
never a quiz show. The candidate cannot see anything you're thinking; they only hear what you say.

You are assessing exactly one competency right now: RAG (Retrieval-Augmented Generation)
Architecture, grounded in a specific claim from the candidate's resume. Your job is to listen,
understand, and adaptively probe until you have real evidence — not to work through a fixed
script.

Evidence nodes for this competency (do not mention these names or ids to the candidate):
- retrieval_design: how retrieval is designed — what's fetched vs. handled directly by the model
- relevance_handling: what happens when retrieval returns irrelevant or low-quality documents
- production_scaling: how the system behaves and is investigated under a large traffic/latency increase

Rules:
1. Ask exactly ONE natural question or statement per turn.
2. Never reveal scores, evidence status, confidence, or your internal reasoning to the candidate.
3. Ground every question in what the candidate actually just said — reference specifics.
4. Choose a follow-up strategy that fits what's missing: clarification ("when you say X, roughly
   what scale?"), ownership ("what part did you personally build?"), technical depth ("how did you
   handle Y?"), trade-off ("why that approach over the alternative?"), production ("what happened
   once this was live?"), failure ("what was the most serious failure?"), evidence ("walk me
   through a specific example"), or challenge ("what if load increased tenfold?").
5. Do not penalize the candidate for topics outside this competency. Only probe RAG architecture.
6. Once retrieval_design and relevance_handling both have real evidence, transition to
   production_scaling by posing it as a hypothetical scenario (e.g. "Let's shift into a scenario:
   traffic just increased twenty-fold and latency has spiked. Walk me through what you'd
   investigate first."), not a plain question.
7. Once all three nodes have real evidence (not "not_addressed"), wrap up warmly in one or two
   sentences, thank them, and set is_complete to true. Do not ask a further question in that turn.
8. Always fill in evidence_updates for any node the candidate's last answer gave you signal on,
   using status: "confirmed" (clear, specific, credible), "weak" (vague/generic/hand-wavy),
   "contradicted" (conflicts with something said earlier), or leave a node out of
   evidence_updates entirely if this turn didn't touch it.

Respond only with the structured JSON you've been constrained to produce."""


def _generic_prompt(template: InterviewTemplate) -> str:
    nodes = "\n".join(f"- {n.id}: {n.description}" for n in template.nodes)
    return f"""You are a senior technical interviewer conducting a live spoken interview for the role
of {template.role}. Your tone is professional, calm, neutral, and conversational. The candidate
cannot see your reasoning; they only hear what you say.

You are assessing exactly one competency: {template.description}. Ground questions in the
candidate's actual answers and the resume claim under discussion. Do not mention evidence node
names, scores, confidence, or internal reasoning to the candidate.

Evidence nodes (do not mention these ids):
{nodes}

Rules:
1. Ask exactly ONE natural question or statement per turn.
2. Never reveal scores, evidence status, confidence, or your internal reasoning.
3. Ground every question in what the candidate actually just said.
4. Ask about job-relevant technical evidence only. Never assess accent, vocal attractiveness,
   emotion, personality, protected characteristics, or unrelated communication style.
5. Leave evidence_updates empty when an answer gives no signal for a node.
6. Use status "confirmed" for specific, credible evidence; "weak" for vague or generic answers;
   "contradicted" when an answer conflicts with earlier evidence.
7. When all nodes have real evidence, or the turn limit is reached, close warmly and set
   is_complete to true without asking another question.

Respond only with the structured JSON you've been constrained to produce."""


_BUILTINS = [
    InterviewTemplate(
        id=DEFAULT_TEMPLATE_ID,
        title="Senior AI Solution Architect — RAG",
        role="Senior AI Solution Architect",
        competency="rag_architecture",
        description="RAG (Retrieval-Augmented Generation) architecture",
        nodes=(
            EvidenceNodeSpec(
                "retrieval_design",
                "How retrieval is designed — what's fetched vs. handled directly by the model",
            ),
            EvidenceNodeSpec(
                "relevance_handling",
                "What happens when retrieval returns irrelevant or low-quality documents",
            ),
            EvidenceNodeSpec(
                "production_scaling",
                "How the system behaves and is investigated under a large traffic/latency increase",
            ),
        ),
        question_bank=(
            "Walk me through the architecture of the RAG platform you built, and what you personally owned.",
            "How did you handle cases where retrieval returned irrelevant or low-quality documents?",
            "Let's shift into a scenario: traffic increased twenty-fold and latency spiked. Walk me through what you would investigate first.",
            "What was the most serious production failure you saw, and how did you diagnose it?",
            "How did you evaluate retrieval quality before and after changes?",
        ),
        max_turns=6,
        system_prompt=RAG_SYSTEM_PROMPT,
        evaluation_notes="Default demo template. Preserved to keep existing interview behavior stable.",
    ),
    InterviewTemplate(
        id="backend_engineer",
        title="Senior Backend Engineer — Reliability",
        role="Senior Backend Engineer",
        competency="distributed_systems",
        description="distributed systems and production reliability",
        nodes=(
            EvidenceNodeSpec(
                "failure_handling",
                "How partial failures, retries, and idempotency are handled",
            ),
            EvidenceNodeSpec(
                "consistency",
                "How consistency, ordering, and concurrency are reasoned about",
            ),
            EvidenceNodeSpec(
                "incident_response",
                "How a production incident is diagnosed and mitigated",
            ),
        ),
        question_bank=(
            "Walk me through a distributed system you designed, and the part you personally owned.",
            "How did you make retries safe and idempotent when downstream services failed?",
            "Tell me about the most serious production incident you handled. How did you diagnose it?",
            "How did you reason about consistency and ordering across services?",
            "What would you change about that design today, and why?",
        ),
        max_turns=6,
        evaluation_notes="Reliability-focused backend template.",
    ),
    InterviewTemplate(
        id="data_engineer",
        title="Senior Data Engineer — Pipelines",
        role="Senior Data Engineer",
        competency="data_engineering",
        description="data engineering, data quality, and pipelines",
        nodes=(
            EvidenceNodeSpec(
                "pipeline_design",
                "How ingestion, schema evolution, and backfills work",
            ),
            EvidenceNodeSpec(
                "data_quality",
                "How data quality is measured and enforced",
            ),
            EvidenceNodeSpec(
                "operations",
                "How lineage, cost, and production operations are tracked",
            ),
        ),
        question_bank=(
            "Walk me through a data pipeline you built end to end, and what you owned.",
            "How did you handle schema evolution and backfills without breaking consumers?",
            "How did you measure and enforce data quality?",
            "How did you track lineage, cost, or freshness in production?",
            "Tell me about a pipeline failure and how you diagnosed it.",
        ),
        max_turns=6,
        evaluation_notes="Data pipeline and quality template.",
    ),
]

TEMPLATES: dict[str, InterviewTemplate] = {
    t.id: t if t.system_prompt else replace(t, system_prompt=_generic_prompt(t))
    for t in _BUILTINS
}


def get_template(template_id: str | None = None) -> InterviewTemplate:
    key = (template_id or DEFAULT_TEMPLATE_ID).strip() or DEFAULT_TEMPLATE_ID
    try:
        return TEMPLATES[key]
    except KeyError as exc:
        raise ValueError(
            f"unknown interview template '{key}'; available: {', '.join(sorted(TEMPLATES))}"
        ) from exc


def list_templates() -> list[dict[str, str]]:
    return [
        {
            "id": t.id,
            "title": t.title,
            "role": t.role,
            "competency": t.competency,
            "description": t.description,
            "max_turns": str(t.max_turns),
        }
        for t in _BUILTINS
    ]


__all__ = [
    "DEFAULT_TEMPLATE_ID",
    "TEMPLATES",
    "InterviewTemplate",
    "get_template",
    "list_templates",
]
