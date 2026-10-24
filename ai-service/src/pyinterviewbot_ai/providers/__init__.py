"""Provider-neutral speech/language interfaces and adapters."""

from __future__ import annotations

from .base import (
    EvidenceNodeSpec,
    LanguageModel,
    ProviderCapabilities,
    ProviderError,
    SpeechToText,
    TextToSpeech,
    TranscriptResult,
    TurnContext,
)
from .registry import (
    configured_providers,
    get_asr,
    get_language_model,
    get_tts,
    provider_status,
    reset_providers,
)

__all__ = [
    "EvidenceNodeSpec",
    "LanguageModel",
    "ProviderCapabilities",
    "ProviderError",
    "SpeechToText",
    "TextToSpeech",
    "TranscriptResult",
    "TurnContext",
    "configured_providers",
    "get_asr",
    "get_language_model",
    "get_tts",
    "provider_status",
    "reset_providers",
]
