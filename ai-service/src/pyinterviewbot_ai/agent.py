"""The interview agent: adaptive question generation + evidence extraction
(spec §13/§14/§30). One LLM call per turn produces both — the gateway only
ever sees the resulting question text and a completion flag; everything
else here stays server-side.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

from . import db, llm
from .evidence import EvidenceGraphState
from .models import TurnResponse
from .resume import seed_claim

MAX_TURNS = int(os.environ.get("PYINTERVIEWBOT_MAX_TURNS", "6"))

FALLBACK_CLOSING_REMARK = (
    "That's a great place to stop — thank you for walking me through all of that in such detail."
)

SYSTEM_PROMPT = """You are a senior technical interviewer conducting a live spoken interview.
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


def _opening_prompt(claim_text: str) -> str:
    return (
        f'The candidate\'s resume includes this claim: "{claim_text}"\n\n'
        "This is the very first turn. Open the interview by warmly referencing this claim and "
        "asking them to walk you through the architecture. Leave evidence_updates empty and "
        "is_complete false."
    )


def _turn_prompt(
    claim_text: str,
    graph: EvidenceGraphState,
    history: list[tuple[str, str]],
    latest_answer: str,
    force_conclude: bool,
) -> str:
    transcript = "\n".join(f"{'Interviewer' if s == 'ai' else 'Candidate'}: {t}" for s, t in history)
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


async def _load_graph(session_id: UUID) -> EvidenceGraphState:
    graph = EvidenceGraphState()
    for record in await db.get_evidence(session_id):
        graph.apply(record.node, record.status, record.note)
    return graph


async def start_session() -> tuple[UUID, str]:
    session_id = uuid4()
    claim = seed_claim()
    await db.create_session(session_id, claim)

    decision = await llm.decide_turn(SYSTEM_PROMPT, _opening_prompt(claim.text))

    await db.append_transcript(session_id, 0, "ai", decision.question_text)
    await db.set_turn_count(session_id, 0)

    return session_id, decision.question_text


async def take_turn(session_id: UUID, transcript: str) -> TurnResponse:
    row = await db.get_session_row(session_id)
    if row is None:
        raise ValueError(f"unknown session {session_id}")

    turn_number = row["turn_count"] + 1
    claim_text = row["resume_claim"]

    await db.append_transcript(session_id, turn_number, "candidate", transcript)

    graph = await _load_graph(session_id)
    history = [(s.speaker, s.text) for s in await db.get_transcript(session_id) if s.turn_number < turn_number]

    force_conclude = turn_number >= MAX_TURNS
    decision = await llm.decide_turn(
        SYSTEM_PROMPT, _turn_prompt(claim_text, graph, history, transcript, force_conclude)
    )

    for update in decision.evidence_updates:
        graph.apply(update.node, update.status, update.note)
        await db.record_evidence(session_id, update.node, update.status, update.note, turn_number)

    is_complete = decision.is_complete or force_conclude
    ai_text = decision.question_text.strip()
    if not ai_text:
        # Defensive fallback, same spirit as the gateway's ASR-failure
        # fallback (spec §28) — the primary path is always real LLM output;
        # this only fires if the model returns nothing for the closing turn.
        ai_text = FALLBACK_CLOSING_REMARK if is_complete else "Could you tell me a bit more about that?"

    await db.append_transcript(session_id, turn_number, "ai", ai_text)
    await db.set_turn_count(session_id, turn_number)

    if is_complete:
        await db.mark_complete(session_id)

    return TurnResponse(ai_text=ai_text, is_complete=is_complete)
