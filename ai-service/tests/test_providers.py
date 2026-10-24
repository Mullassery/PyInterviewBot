"""Provider contract tests for the deterministic mock and local adapters."""

from __future__ import annotations

import pytest

from pyinterviewbot_ai import providers
from pyinterviewbot_ai.providers.base import TurnContext
from pyinterviewbot_ai.providers.mock import (
    MOCK_CLOSING,
    MockLanguageModel,
    MockSpeechToText,
    MockTextToSpeech,
)
from pyinterviewbot_ai.templates import get_template


async def test_mock_speech_contracts() -> None:
    asr = MockSpeechToText(["hello world"])
    result = await asr.transcribe(b"\x00\x00" * 160)
    assert result.text == "hello world"
    assert result.is_final is True
    assert asr.capabilities().kind == "asr"

    tts = MockTextToSpeech()
    chunks = [chunk async for chunk in tts.synthesize("hello")]
    assert chunks
    assert all(len(chunk) % 2 == 0 for chunk in chunks)
    assert len(b"".join(chunks)) > 0


async def test_mock_planner_is_deterministic_and_completes() -> None:
    template = get_template("backend_engineer")
    planner = MockLanguageModel()
    context = TurnContext(
        template_id=template.id,
        competency=template.competency,
        evidence_nodes=template.nodes,
        evidence_statuses={},
        question_bank=template.question_bank,
        turn_number=0,
    )
    opening = await planner.decide("sys", "user", context)
    assert opening.question_text == template.question_bank[0]
    assert opening.is_complete is False

    final_context = TurnContext(
        template_id=template.id,
        competency=template.competency,
        evidence_nodes=template.nodes,
        evidence_statuses={node.id: "confirmed" for node in template.nodes},
        question_bank=template.question_bank,
        turn_number=3,
    )
    final = await planner.decide("sys", "user", final_context)
    assert final.is_complete is True
    assert final.question_text == MOCK_CLOSING


async def test_registry_fails_closed_on_unknown_provider(monkeypatch) -> None:
    monkeypatch.setenv("PYINTERVIEWBOT_ASR_PROVIDER", "does-not-exist")
    providers.reset_providers()
    with pytest.raises(ValueError, match="unknown asr provider"):
        providers.get_asr()
