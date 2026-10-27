"""OpenRouter OpenAI-compatible provider contract tests."""

from __future__ import annotations

import json

import httpx
import pytest
from pyinterviewbot_ai import providers
from pyinterviewbot_ai.providers.base import ProviderError, TurnContext
from pyinterviewbot_ai.providers.openrouter import OpenRouterLanguageModel

DECISION = {
    "evidence_updates": [],
    "next_action": "probe",
    "question_text": "Walk me through a specific example.",
    "is_complete": False,
}


def _context() -> TurnContext:
    return TurnContext(
        template_id="test",
        competency="test",
        evidence_nodes=(),
        latest_answer="answer",
    )


async def test_openrouter_request_contract() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers["authorization"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(DECISION)}}]},
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openrouter.test/api/v1"
    )
    model = OpenRouterLanguageModel(
        api_key="secret",
        model="anthropic/claude-3.5-sonnet",
        base_url="https://openrouter.test/api/v1",
        client=client,
    )
    result = await model.decide("system", "user", _context())
    assert result.question_text == DECISION["question_text"]
    assert captured["path"] == "/api/v1/chat/completions"
    assert captured["auth"] == "Bearer secret"
    body = captured["body"]
    assert body["model"] == "anthropic/claude-3.5-sonnet"
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0] == {"role": "system", "content": "system"}
    await client.aclose()


async def test_openrouter_retries_retryable_errors() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(DECISION)}}]}
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openrouter.test/api/v1"
    )
    model = OpenRouterLanguageModel(
        api_key="secret",
        model="model",
        base_url="https://openrouter.test/api/v1",
        max_retries=1,
        retry_base_delay=0,
        client=client,
    )
    result = await model.decide("system", "user", _context())
    assert result.is_complete is False
    assert calls == 2
    await client.aclose()


async def test_openrouter_rejects_invalid_json() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "not json"}}]}
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openrouter.test/api/v1"
    )
    model = OpenRouterLanguageModel(
        api_key="secret",
        model="model",
        base_url="https://openrouter.test/api/v1",
        max_retries=0,
        client=client,
    )
    with pytest.raises(ProviderError) as excinfo:
        await model.decide("system", "user", _context())
    assert excinfo.value.provider == "openrouter"
    assert excinfo.value.retryable is False
    await client.aclose()


async def test_openrouter_health() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openrouter.test/api/v1"
    )
    model = OpenRouterLanguageModel(
        api_key="secret", model="model", base_url="https://openrouter.test/api/v1", client=client
    )
    assert await model.health() is True
    await client.aclose()


def test_openrouter_requires_key_and_model(monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        OpenRouterLanguageModel()
    with pytest.raises(ValueError, match="OPENROUTER_MODEL"):
        OpenRouterLanguageModel(api_key="key")


async def test_registry_selects_openrouter(monkeypatch) -> None:
    monkeypatch.setenv("PYINTERVIEWBOT_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    monkeypatch.setenv("OPENROUTER_MODEL", "vendor/model")
    providers.reset_providers()
    model = providers.get_language_model()
    assert model.capabilities().name == "openrouter:vendor/model"
    providers.reset_providers()


async def test_openrouter_falls_back_when_model_rejects_response_format() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        if "response_format" in body:
            return httpx.Response(400, json={"error": "response_format unsupported"})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(DECISION)}}]}
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openrouter.test/api/v1"
    )
    model = OpenRouterLanguageModel(
        api_key="secret",
        model="meta-llama/llama-3.3-70b-instruct",
        base_url="https://openrouter.test/api/v1",
        client=client,
    )
    result = await model.decide("system", "user", _context())
    assert result.question_text == DECISION["question_text"]
    assert calls == 2
    await client.aclose()


async def test_openrouter_normalizes_nonconforming_model_output() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "evidence_updates": [],
                                    "next_action": "ask_question",
                                    "question": "What did you own?",
                                    "is_complete": False,
                                }
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openrouter.test/api/v1"
    )
    model = OpenRouterLanguageModel(
        api_key="secret",
        model="nvidia/nemotron-3-ultra-550b-a55b:free",
        base_url="https://openrouter.test/api/v1",
        client=client,
    )
    result = await model.decide("system", "user", _context())
    assert result.next_action == "probe"
    assert result.question_text == "What did you own?"
    await client.aclose()


async def test_openrouter_stt_request_and_response() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers["authorization"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"text": "candidate answer"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openrouter.test/api/v1"
    )
    from pyinterviewbot_ai.providers.openrouter import OpenRouterSpeechToText

    stt = OpenRouterSpeechToText(
        api_key="secret",
        model="openai/whisper-1",
        base_url="https://openrouter.test/api/v1",
        client=client,
    )
    result = await stt.transcribe(b"\x00\x00" * 1600)
    assert result.text == "candidate answer"
    assert captured["path"] == "/api/v1/audio/transcriptions"
    assert captured["auth"] == "Bearer secret"
    body = captured["body"]
    assert body["model"] == "openai/whisper-1"
    assert body["input_audio"]["format"] == "wav"
    await client.aclose()


async def test_openrouter_tts_request_and_resample() -> None:
    captured: dict[str, object] = {}
    pcm = b"\x00\x00" * 24_000  # 1s at 24kHz

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers["authorization"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=pcm)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openrouter.test/api/v1"
    )
    from pyinterviewbot_ai.providers.openrouter import OpenRouterTextToSpeech

    tts = OpenRouterTextToSpeech(
        api_key="secret",
        model="openai/gpt-4o-mini-tts",
        voice="alloy",
        base_url="https://openrouter.test/api/v1",
        source_sample_rate=24_000,
        client=client,
    )
    audio = b"".join([chunk async for chunk in tts.synthesize("Hello")])
    assert len(audio) == 22_050 * 2
    assert captured["path"] == "/api/v1/audio/speech"
    assert captured["auth"] == "Bearer secret"
    body = captured["body"]
    assert body["model"] == "openai/gpt-4o-mini-tts"
    assert body["voice"] == "alloy"
    assert body["response_format"] == "pcm"
    await client.aclose()


async def test_registry_selects_openrouter_speech(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    monkeypatch.setenv("OPENROUTER_STT_MODEL", "openai/whisper-1")
    monkeypatch.setenv("OPENROUTER_TTS_MODEL", "openai/gpt-4o-mini-tts")
    monkeypatch.setenv("PYINTERVIEWBOT_ASR_PROVIDER", "openrouter")
    monkeypatch.setenv("PYINTERVIEWBOT_TTS_PROVIDER", "openrouter")
    providers.reset_providers()
    assert providers.get_asr().capabilities().name == "openrouter-stt:openai/whisper-1"
    assert providers.get_tts().capabilities().name == "openrouter-tts:openai/gpt-4o-mini-tts"
    providers.reset_providers()
