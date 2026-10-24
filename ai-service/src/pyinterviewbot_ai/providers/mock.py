"""Deterministic providers for CI and offline development.

Mock mode never loads a model, contacts a network service, or requires a
credential. It is intentionally simple and should not be presented as real
speech recognition or synthesis.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

from ..models import AgentTurnDecision, EvidenceUpdate
from .base import (
    ProviderCapabilities,
    TranscriptResult,
    TurnContext,
)

MOCK_CLOSING = "Thank you — that gives me enough evidence for this interview. We can stop here."
MOCK_DEFAULT_TRANSCRIPT = "This is a deterministic mock transcript used only for tests."
MOCK_SAMPLE_RATE = 22_050


class MockSpeechToText:
    def __init__(self, transcripts: list[str] | None = None) -> None:
        configured = os.environ.get("MOCK_ASR_TRANSCRIPTS", "")
        self._transcripts = (
            transcripts
            if transcripts is not None
            else [item.strip() for item in configured.split("|") if item.strip()]
        )
        self._index = 0

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name="mock-asr",
            kind="asr",
            local=True,
            notes="Returns canned text; never transcribes real audio.",
        )

    async def health(self) -> bool:
        return True

    async def transcribe(
        self, pcm16_16khz: bytes, *, language: str = "en"
    ) -> TranscriptResult:
        if self._transcripts:
            text = self._transcripts[min(self._index, len(self._transcripts) - 1)]
            self._index += 1
        else:
            text = MOCK_DEFAULT_TRANSCRIPT
        return TranscriptResult(text=text, language=language, confidence=1.0)


class MockTextToSpeech:
    sample_rate = MOCK_SAMPLE_RATE

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name="mock-tts",
            kind="tts",
            local=True,
            streaming_output=True,
            notes="Emits deterministic silence; not audible speech.",
        )

    async def health(self) -> bool:
        return True

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        duration_ms = min(3_000, 250 + len(text) * 15)
        total_bytes = int(self.sample_rate * duration_ms / 1000) * 2
        chunk_size = 4096
        for start in range(0, total_bytes, chunk_size):
            yield b"\x00\x00" * ((min(chunk_size, total_bytes - start)) // 2)


class MockLanguageModel:
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name="mock-llm",
            kind="llm",
            local=True,
            notes="Deterministic evidence/sequence decisions; no model inference.",
        )

    async def health(self) -> bool:
        return True

    async def decide(
        self,
        system_prompt: str,
        user_prompt: str,
        context: TurnContext,
    ) -> AgentTurnDecision:
        if context.turn_number == 0:
            opening = (
                context.question_bank[0]
                if context.question_bank
                else "Tell me about the work you are most proud of."
            )
            return AgentTurnDecision(
                evidence_updates=[],
                next_action="probe",
                question_text=opening,
                is_complete=False,
            )
        if context.force_conclude or context.turn_number >= context.max_turns:
            return AgentTurnDecision(
                evidence_updates=[],
                next_action="conclude",
                question_text=MOCK_CLOSING,
                is_complete=True,
            )

        node_ids = [node.id for node in context.evidence_nodes]
        addressed = {
            node_id
            for node_id, status in context.evidence_statuses.items()
            if status and status != "not_addressed"
        }
        pending = [node_id for node_id in node_ids if node_id not in addressed]

        if not pending:
            return AgentTurnDecision(
                evidence_updates=[],
                next_action="conclude",
                question_text=MOCK_CLOSING,
                is_complete=True,
            )

        node_id = pending[0]
        word_count = len(context.latest_answer.split())
        status = "confirmed" if word_count >= 8 else "weak"
        note = (
            f"Candidate answer on turn {context.turn_number} "
            + ("contained specific detail." if status == "confirmed" else "was generic.")
        )
        remaining = pending[1:]
        if not remaining:
            return AgentTurnDecision(
                evidence_updates=[EvidenceUpdate(node=node_id, status=status, note=note)],
                next_action="conclude",
                question_text=MOCK_CLOSING,
                is_complete=True,
            )

        index = min(len(node_ids) - len(pending) + 1, len(context.question_bank) - 1)
        question = context.question_bank[index] if context.question_bank else (
            "Could you go deeper into that with a specific example?"
        )
        return AgentTurnDecision(
            evidence_updates=[EvidenceUpdate(node=node_id, status=status, note=note)],
            next_action="probe",
            question_text=question,
            is_complete=False,
        )


__all__ = [
    "MOCK_CLOSING",
    "MOCK_DEFAULT_TRANSCRIPT",
    "MockLanguageModel",
    "MockSpeechToText",
    "MockTextToSpeech",
]
