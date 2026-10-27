"""Sandbox and production bindings are explicit and fingerprinted."""

from __future__ import annotations

import pytest
from pyinterviewbot_ai.workflow_env import (
    BindingError,
    production_binding,
    sandbox_binding,
)
from pyinterviewbot_ai.workflow_models import RuntimeRequirements


def test_sandbox_binding_is_fixed_to_mock_providers() -> None:
    binding = sandbox_binding()
    assert binding.environment == "sandbox"
    assert binding.providers == {"llm": "mock", "asr": "mock", "tts": "mock"}
    assert binding.supports(RuntimeRequirements(capabilities=["llm", "asr", "tts"]))
    assert binding.fingerprint


def test_production_binding_requires_an_explicit_revision(monkeypatch) -> None:
    monkeypatch.delenv("PYINTERVIEWBOT_PRODUCTION_BINDING_REVISION", raising=False)
    with pytest.raises(BindingError):
        production_binding()


def test_production_binding_rejects_an_unknown_provider(monkeypatch) -> None:
    monkeypatch.setenv("PYINTERVIEWBOT_PRODUCTION_BINDING_REVISION", "prod-2026-10")
    monkeypatch.setenv("PYINTERVIEWBOT_LLM_PROVIDER", "does-not-exist")

    with pytest.raises(BindingError, match="unknown production provider selection"):
        production_binding()


def test_production_binding_fingerprint_tracks_revision_and_providers(monkeypatch) -> None:
    monkeypatch.setenv("PYINTERVIEWBOT_PRODUCTION_BINDING_REVISION", "prod-2026-10")
    monkeypatch.setenv("PYINTERVIEWBOT_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("PYINTERVIEWBOT_ASR_PROVIDER", "local")
    monkeypatch.setenv("PYINTERVIEWBOT_TTS_PROVIDER", "local")
    first = production_binding()

    monkeypatch.setenv("PYINTERVIEWBOT_ASR_PROVIDER", "openrouter")
    second = production_binding()
    assert first.environment == "production"
    assert first.revision == "prod-2026-10"
    assert first.fingerprint != second.fingerprint

    monkeypatch.setenv("PYINTERVIEWBOT_PRODUCTION_BINDING_REVISION", "prod-2026-11")
    third = production_binding()
    assert second.fingerprint != third.fingerprint
