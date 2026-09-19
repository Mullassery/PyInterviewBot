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


class StartSessionResponse(BaseModel):
    session_id: UUID
    question_text: str


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
    is_complete: bool
    turn_count: int


class SessionSummary(BaseModel):
    """Recruiter-facing view — the one place all this detail is allowed to
    surface (spec §24)."""

    session_id: UUID
    resume_claim: str
    is_complete: bool
    transcript: list[TranscriptSegment]
    evidence: list[EvidenceRecord]
