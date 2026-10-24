"""The interview agent: adaptive question generation + evidence extraction.

Business logic lives here: templates, sequencing, evidence ownership, turn
limits, and completion. Providers only convert speech or return structured
decisions.
"""

from __future__ import annotations

import logging
import os
import secrets
from uuid import UUID, uuid4

from . import db
from .auth import hash_session_token
from .compliance import (
    SAFE_FALLBACK_QUESTIONS,
    CompliancePolicy,
    check_question,
    load_policy,
)
from .evidence import EvidenceGraphState
from .models import TurnResponse
from .providers import EvidenceNodeSpec, TurnContext, get_language_model
from .resume import seed_claim
from .templates import (
    DEFAULT_TEMPLATE_ID,
    RAG_SYSTEM_PROMPT,
    InterviewTemplate,
    get_template,
)

SYSTEM_PROMPT = RAG_SYSTEM_PROMPT
DEFAULT_MAX_TURNS = 6

FALLBACK_CLOSING_REMARK = (
    "That's a great place to stop — thank you for walking me through all of that in such detail."
)
logger = logging.getLogger(__name__)


def _safe_question(
    text: str,
    template: InterviewTemplate,
    history: list[tuple[str, str]],
    is_complete: bool,
    policy: CompliancePolicy,
) -> str:
    decision = check_question(text, policy)
    if decision.allowed:
        return text
    logger.warning(
        "blocked non-compliant candidate question; categories=%s",
        ",".join(decision.categories),
    )
    if is_complete:
        return FALLBACK_CLOSING_REMARK
    asked = {content for speaker, content in history if speaker == "ai"}
    for candidate in (*template.question_bank, *SAFE_FALLBACK_QUESTIONS):
        if candidate not in asked and check_question(candidate, policy).allowed:
            return candidate
    return SAFE_FALLBACK_QUESTIONS[0]


def _max_turns(template: InterviewTemplate) -> int:
    raw = os.environ.get("PYINTERVIEWBOT_MAX_TURNS")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return template.max_turns or DEFAULT_MAX_TURNS


def _opening_prompt(claim_text: str, template: InterviewTemplate) -> str:
    if template.id == DEFAULT_TEMPLATE_ID:
        return (
            f'The candidate\'s resume includes this claim: "{claim_text}"\n\n'
            "This is the very first turn. Open the interview by warmly referencing this claim and "
            "asking them to walk you through the architecture. Leave evidence_updates empty and "
            "is_complete false."
        )
    return (
        f'The candidate\'s resume includes this claim: "{claim_text}"\n\n'
        f"This is the very first turn for a {template.role} interview focused on "
        f"{template.description}. Open by warmly referencing the claim and asking the candidate "
        "to walk you through the work they personally owned. Leave evidence_updates empty and "
        "is_complete false."
    )


def _turn_prompt(
    claim_text: str,
    graph: EvidenceGraphState,
    history: list[tuple[str, str]],
    latest_answer: str,
    force_conclude: bool,
) -> str:
    transcript = "\n".join(
        f"{'Interviewer' if s == 'ai' else 'Candidate'}: {t}" for s, t in history
    )
    closing_instruction = (
        "\n\nThis is the FINAL turn regardless of evidence coverage — the interview time is up. "
        "question_text must NOT be a question — it must be 1-2 warm closing sentences thanking the "
        "candidate and telling them the interview is complete. question_text must not be empty. "
        "Set is_complete to true."
        if force_conclude
        else ""
    )
    return (
        f'Resume claim under discussion: "{claim_text}"\n\n'
        f"Evidence gathered so far:\n{graph.summary_for_prompt()}\n\n"
        f"Conversation so far:\n{transcript}\n\n"
        f"Candidate's latest answer: \"{latest_answer}\"\n\n"
        "Decide evidence_updates from that answer, then produce your next question per the rules."
        f"{closing_instruction}"
    )


def _node_specs(template: InterviewTemplate) -> dict[str, str]:
    return {node.id: node.description for node in template.nodes}


def _evidence_nodes(template: InterviewTemplate) -> tuple[EvidenceNodeSpec, ...]:
    return template.nodes


def _context(
    template: InterviewTemplate,
    graph: EvidenceGraphState,
    history: list[tuple[str, str]],
    latest_answer: str,
    turn_number: int,
    force_conclude: bool,
) -> TurnContext:
    return TurnContext(
        template_id=template.id,
        competency=template.competency,
        evidence_nodes=_evidence_nodes(template),
        evidence_statuses=graph.statuses(),
        question_bank=template.question_bank,
        turn_number=turn_number,
        max_turns=_max_turns(template),
        force_conclude=force_conclude,
        latest_answer=latest_answer,
        history=tuple(history),
    )


async def _load_graph(session_id: UUID, template: InterviewTemplate) -> EvidenceGraphState:
    graph = EvidenceGraphState(node_specs=_node_specs(template))
    for record in await db.get_evidence(session_id):
        graph.apply(record.node, record.status, record.note)
    return graph


async def start_session(
    template_id: str | None = None,
) -> tuple[UUID, str, str, str]:
    template = get_template(template_id)
    session_id = uuid4()
    claim = seed_claim(template.competency)
    session_token = secrets.token_urlsafe(32)
    await db.create_session(
        session_id,
        claim,
        template.id,
        token_hash=hash_session_token(session_token),
    )

    graph = EvidenceGraphState(node_specs=_node_specs(template))
    planner = get_language_model()
    decision = await planner.decide(
        template.system_prompt,
        _opening_prompt(claim.text, template),
        _context(template, graph, [], "", 0, False),
    )

    opening = _safe_question(
        decision.question_text, template, [], False, await load_policy()
    )
    await db.append_transcript(session_id, 0, "ai", opening)
    await db.set_turn_count(session_id, 0)

    return session_id, opening, template.id, session_token


async def take_turn(session_id: UUID, transcript: str) -> TurnResponse:
    row = await db.get_session_row(session_id)
    if row is None:
        raise ValueError(f"unknown session {session_id}")

    template = get_template(row["template_id"] or DEFAULT_TEMPLATE_ID)
    turn_number = row["turn_count"] + 1
    claim_text = row["resume_claim"]

    await db.append_transcript(session_id, turn_number, "candidate", transcript)

    graph = await _load_graph(session_id, template)
    history = [
        (s.speaker, s.text)
        for s in await db.get_transcript(session_id)
        if s.turn_number < turn_number
    ]

    force_conclude = turn_number >= _max_turns(template)
    planner = get_language_model()
    decision = await planner.decide(
        template.system_prompt,
        _turn_prompt(claim_text, graph, history, transcript, force_conclude),
        _context(
            template,
            graph,
            history,
            transcript,
            turn_number,
            force_conclude,
        ),
    )

    valid_nodes = {node.id for node in template.nodes}
    for update in decision.evidence_updates:
        if update.node not in valid_nodes:
            continue
        graph.apply(update.node, update.status, update.note)
        await db.record_evidence(
            session_id,
            update.node,
            update.status,
            update.note,
            turn_number,
            competency=template.competency,
        )

    is_complete = decision.is_complete or force_conclude
    ai_text = decision.question_text.strip()
    if not ai_text:
        ai_text = (
            FALLBACK_CLOSING_REMARK
            if is_complete
            else "Could you tell me a bit more about that?"
        )

    ai_text = _safe_question(
        ai_text, template, history, is_complete, await load_policy()
    )
    await db.append_transcript(session_id, turn_number, "ai", ai_text)
    await db.set_turn_count(session_id, turn_number)

    if is_complete:
        await db.mark_complete(session_id)

    return TurnResponse(ai_text=ai_text, is_complete=is_complete)
