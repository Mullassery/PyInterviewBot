"""Environment-driven provider registry.

Selection is explicit and provider-neutral: ``PYINTERVIEWBOT_ASR_PROVIDER``,
``PYINTERVIEWBOT_TTS_PROVIDER``, and ``PYINTERVIEWBOT_LLM_PROVIDER`` accept
``local``, ``mock``, or ``voxora`` (LLM only). Unknown values fail closed
with a clear error instead of silently falling back.
"""

from __future__ import annotations

import os
from typing import Any

from .base import (
    LanguageModel,
    ProviderCapabilities,
    ProviderError,
    SpeechToText,
    TextToSpeech,
)

_CACHE: dict[tuple[str, str], Any] = {}

_ENV_BY_KIND = {
    "asr": "PYINTERVIEWBOT_ASR_PROVIDER",
    "tts": "PYINTERVIEWBOT_TTS_PROVIDER",
    "llm": "PYINTERVIEWBOT_LLM_PROVIDER",
}

_KNOWN_PROVIDERS = {
    "asr": frozenset({"local", "mock"}),
    "tts": frozenset({"local", "mock"}),
    "llm": frozenset({"local", "mock", "voxora"}),
}

# Archived after the ecosystem migration: Voxora Studio owns OpenRouter
# connectivity, model routing and billing (see docs/migration.md).
_REMOVED_PROVIDERS = {
}

_MIGRATION_POINTER = (
    "OpenRouter was moved to Voxora Studio. Set PYINTERVIEWBOT_LLM_PROVIDER=voxora "
    "and configure VOXORA_BASE_URL/VOXORA_API_KEY."
)


def provider_is_known(kind: str, provider: str) -> bool:
    return provider in _KNOWN_PROVIDERS.get(kind, frozenset())


def _select(kind: str) -> str:
    env_name = _ENV_BY_KIND[kind]
    value = os.environ.get(env_name) or os.environ.get("PYINTERVIEWBOT_PROVIDER") or "local"
    return value.strip().lower()


def _build(kind: str, provider: str) -> Any:
    if provider == "mock":
        from .mock import MockLanguageModel, MockSpeechToText, MockTextToSpeech

        return {
            "asr": MockSpeechToText,
            "tts": MockTextToSpeech,
            "llm": MockLanguageModel,
        }[kind]()
    if provider == "local":
        from .local import LocalLanguageModel, LocalSpeechToText, LocalTextToSpeech

        return {
            "asr": LocalSpeechToText,
            "tts": LocalTextToSpeech,
            "llm": LocalLanguageModel,
        }[kind]()
    if provider == "voxora" and kind == "llm":
        from .voxora import VoxoraLanguageModel

        return VoxoraLanguageModel()
    if provider == "voxora" and kind in {"asr", "tts"}:
        raise ValueError(
            "voxora handles speech inside its voice sessions; PyInterviewBot consumes "
            "transcripts. Use 'local' or 'mock' for PYINTERVIEWBOT_"
            f"{kind.upper()}_PROVIDER."
        )
    if provider == "openrouter":
        raise ValueError(_MIGRATION_POINTER)
    raise ValueError(f"unknown {kind} provider '{provider}'; expected local, mock, or voxora")


def _get(kind: str) -> Any:
    provider = _select(kind)
    key = (kind, provider)
    if key not in _CACHE:
        _CACHE[key] = _build(kind, provider)
    return _CACHE[key]


def get_asr() -> SpeechToText:
    return _get("asr")


def get_tts() -> TextToSpeech:
    return _get("tts")


def get_language_model() -> LanguageModel:
    return _get("llm")


def reset_providers() -> None:
    _CACHE.clear()


def configured_providers() -> dict[str, str]:
    return {kind: _select(kind) for kind in _ENV_BY_KIND}


async def provider_status() -> list[dict[str, Any]]:
    statuses: list[dict[str, Any]] = []
    for kind in ("asr", "tts", "llm"):
        provider = _select(kind)
        try:
            instance = _get(kind)
            capabilities = instance.capabilities()
            healthy = await instance.health()
            statuses.append(
                {
                    "kind": kind,
                    "provider": provider,
                    "healthy": healthy,
                    "capabilities": capabilities.__dict__,
                    "error": None,
                }
            )
        except (ValueError, ProviderError) as exc:
            statuses.append(
                {
                    "kind": kind,
                    "provider": provider,
                    "healthy": False,
                    "capabilities": ProviderCapabilities(
                        name=provider, kind=kind
                    ).__dict__,
                    "error": str(exc),
                }
            )
    return statuses


__all__ = [
    "configured_providers",
    "get_asr",
    "get_language_model",
    "get_tts",
    "provider_is_known",
    "provider_status",
    "reset_providers",
    "transcribe_media",
]


async def transcribe_media(data: bytes, suffix: str = ".ogg") -> str:
    provider = _select("asr")
    if provider == "mock":
        from .mock import MockSpeechToText

        result = await MockSpeechToText().transcribe(data)
        return result.text
    if provider == "local":
        from .. import asr

        return await asr.transcribe_media(data, suffix)
    raise ValueError(f"ASR provider '{provider}' does not support media transcription")
