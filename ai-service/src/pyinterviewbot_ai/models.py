"""Pydantic schemas: the HTTP contract with the Rust gateway, plus the
structured-output schema the LLM is constrained to when deciding a turn.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

EvidenceStatus = Literal["confirmed", "weak", "contradicted", "not_addressed"]
NextAction = Literal["probe", "clarify", "challenge", "move_on", "simulate", "conclude"]


class StartSessionRequest(BaseModel):
    resume_seed: bool = True
    template_id: str | None = None


class StartSessionResponse(BaseModel):
    session_id: UUID
    question_text: str
    template_id: str = "rag_architect"


class TurnRequest(BaseModel):
    transcript: str


class TurnResponse(BaseModel):
    ai_text: str
    is_complete: bool


class TranscribeResponse(BaseModel):
    transcript: str


class SynthesizeRequest(BaseModel):
    text: str


class EvidenceUpdate(BaseModel):
    """One evidence-graph node update the LLM produces after hearing an answer."""

    node: str = Field(description="Evidence node id this update applies to")
    status: EvidenceStatus
    note: str = Field(description="One sentence: what the candidate said that supports this status")


class AgentTurnDecision(BaseModel):
    """The full structured output the interview agent LLM call must produce
    each turn. The gateway never sees this — only `question_text` (as
    `ai_text`) and `is_complete` cross the HTTP boundary back to it.
    """

    evidence_updates: list[EvidenceUpdate]
    next_action: NextAction
    question_text: str
    is_complete: bool


class TranscriptSegment(BaseModel):
    turn_number: int
    speaker: Literal["ai", "candidate"]
    text: str


class EvidenceRecord(BaseModel):
    competency: str
    node: str
    status: EvidenceStatus
    note: str
    turn_number: int


class SessionListItem(BaseModel):
    session_id: UUID
    created_at: str
    resume_claim: str
    template_id: str = "rag_architect"
    is_complete: bool
    turn_count: int


class SessionSummary(BaseModel):
    """Recruiter-facing view — the one place all this detail is allowed to
    surface (spec §24)."""

    session_id: UUID
    template_id: str = "rag_architect"
    resume_claim: str
    is_complete: bool
    transcript: list[TranscriptSegment]
    evidence: list[EvidenceRecord]


class ReviewRequest(BaseModel):
    node: str
    reviewer: str
    decision: Literal["confirm", "reject", "needs_more_evidence"]
    note: str = ""


class ReviewCorrection(BaseModel):
    node: str
    reviewer: str
    decision: Literal["confirm", "reject", "needs_more_evidence"]
    note: str
    created_at: str


class EvidenceCitation(BaseModel):
    node: str
    status: EvidenceStatus
    transcript_turn: int
    quote: str
    note: str
    note_is_model_inference: bool = True
    human_decision: Literal["confirm", "reject", "needs_more_evidence"] | None = None


class NodeAssessment(BaseModel):
    node: str
    description: str
    status: EvidenceStatus
    evidence_strength: int | None = Field(default=None, ge=0, le=3)
    max_evidence_strength: int = 3
    uncertainty: Literal["low", "medium", "high"]
    citations: list[EvidenceCitation] = Field(default_factory=list)
    human_review: list[ReviewCorrection] = Field(default_factory=list)


class SessionReport(BaseModel):
    session_id: UUID
    template_id: str
    competency: str
    generated_from: str = "persisted transcript and evidence"
    node_assessments: list[NodeAssessment]
    evidence_coverage: float = Field(ge=0.0, le=1.0)
    contradictions: int
    overall_uncertainty: Literal["low", "medium", "high"]
    human_review_required: bool = True
    decision_support_only: bool = True
    limitations: list[str] = Field(default_factory=list)
