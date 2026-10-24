"""Wrappers around the existing local faster-whisper, Ollama, and Piper stack."""

from __future__ import annotations

from collections.abc import AsyncIterator

from .. import asr, llm, tts
from ..models import AgentTurnDecision
from .base import (
    ProviderCapabilities,
    TranscriptResult,
    TurnContext,
)


class LocalSpeechToText:
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name="local-faster-whisper",
            kind="asr",
            local=True,
            streaming_input=False,
            notes="Per-utterance transcription; models are loaded lazily.",
        )

    async def health(self) -> bool:
        return True

    async def transcribe(
        self, pcm16_16khz: bytes, *, language: str = "en"
    ) -> TranscriptResult:
        text = await asr.transcribe(pcm16_16khz)
        return TranscriptResult(text=text, language=language)


class LocalTextToSpeech:
    sample_rate = tts.SAMPLE_RATE

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name="local-piper",
            kind="tts",
            local=True,
            streaming_output=True,
            notes="Piper streams PCM16 chunks as they are synthesized.",
        )

    async def health(self) -> bool:
        return True

    def synthesize(self, text: str) -> AsyncIterator[bytes]:
        return tts.synthesize_stream(text)


class LocalLanguageModel:
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name="local-ollama",
            kind="llm",
            local=True,
            notes="Structured JSON output constrained by a Pydantic schema.",
        )

    async def health(self) -> bool:
        return True

    async def decide(
        self,
        system_prompt: str,
        user_prompt: str,
        context: TurnContext,
    ) -> AgentTurnDecision:
        return await llm.decide_turn(system_prompt, user_prompt)


__all__ = [
    "LocalLanguageModel",
    "LocalSpeechToText",
    "LocalTextToSpeech",
]
