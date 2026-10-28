"""Provider-neutral speech and language contracts.

Business logic (interview sequencing, evidence ownership) stays in this
package's callers. Providers only convert audio/text and return structured
decisions, so a local engine or a deterministic mock can be swapped without
changing interview state.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..models import AgentTurnDecision


class ProviderError(RuntimeError):
    """Normalized provider failure with explicit retry semantics."""

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        """Record the provider, message, and retry metadata."""
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.message = message
        self.retryable = retryable
        self.status_code = status_code


@dataclass(frozen=True)
class ProviderCapabilities:
    name: str
    kind: str
    local: bool = False
    streaming_input: bool = False
    streaming_output: bool = False
    languages: tuple[str, ...] = ("en",)
    notes: str = ""


@dataclass(frozen=True)
class TranscriptResult:
    text: str
    is_final: bool = True
    language: str | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class EvidenceNodeSpec:
    id: str
    description: str


@dataclass(frozen=True)
class TurnContext:
    """Provider-neutral facts the language model needs for one turn.

    The agent owns this context and the prompts; providers must not become
    the source of truth for interview state.
    """

    template_id: str
    competency: str
    evidence_nodes: tuple[EvidenceNodeSpec, ...]
    evidence_statuses: dict[str, str] = field(default_factory=dict)
    question_bank: tuple[str, ...] = ()
    turn_number: int = 0
    max_turns: int = 6
    force_conclude: bool = False
    latest_answer: str = ""
    history: tuple[tuple[str, str], ...] = ()


@runtime_checkable
class SpeechToText(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...

    async def health(self) -> bool: ...

    async def transcribe(
        self, pcm16_16khz: bytes, *, language: str = "en"
    ) -> TranscriptResult: ...


@runtime_checkable
class TextToSpeech(Protocol):
    sample_rate: int

    def capabilities(self) -> ProviderCapabilities: ...

    async def health(self) -> bool: ...

    def synthesize(self, text: str) -> AsyncIterator[bytes]: ...


@runtime_checkable
class LanguageModel(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...

    async def health(self) -> bool: ...

    async def decide(
        self,
        system_prompt: str,
        user_prompt: str,
        context: TurnContext,
    ) -> AgentTurnDecision: ...


__all__ = [
    "EvidenceNodeSpec",
    "LanguageModel",
    "ProviderCapabilities",
    "ProviderError",
    "SpeechToText",
    "TextToSpeech",
    "TranscriptResult",
    "TurnContext",
]
