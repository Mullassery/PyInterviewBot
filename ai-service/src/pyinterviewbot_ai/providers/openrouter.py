"""OpenRouter OpenAI-compatible chat-completions provider.

The model is explicit (`OPENROUTER_MODEL`) so changing models is a deliberate
configuration change. API keys stay server-side; no key is logged or returned.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import httpx

from ..models import AgentTurnDecision
from .base import ProviderCapabilities, ProviderError, TurnContext

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
logger = logging.getLogger(__name__)
_NEXT_ACTIONS = {"probe", "clarify", "challenge", "move_on", "simulate", "conclude"}
_EVIDENCE_STATUSES = {"confirmed", "weak", "contradicted", "not_addressed"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _normalize_decision(payload: Any) -> AgentTurnDecision:
    if not isinstance(payload, dict):
        raise TypeError("model output is not a JSON object")
    data = dict(payload)
    updates = data.get("evidence_updates")
    normalized_updates: list[dict[str, str]] = []
    if isinstance(updates, list):
        for update in updates:
            if not isinstance(update, dict):
                continue
            node = update.get("node")
            status = update.get("status")
            note = update.get("note", "")
            if isinstance(node, str) and status in _EVIDENCE_STATUSES:
                normalized_updates.append(
                    {"node": node, "status": status, "note": str(note)}
                )
    data["evidence_updates"] = normalized_updates

    action = data.get("next_action", data.get("action", "probe"))
    action = str(action).strip().lower()
    if action not in _NEXT_ACTIONS:
        logger.warning("normalizing unknown OpenRouter next_action %r", action)
        action = "conclude" if data.get("is_complete") else "probe"
    data["next_action"] = action
    data["question_text"] = str(
        data.get("question_text", data.get("question", data.get("text", "")))
    )
    data["is_complete"] = bool(data.get("is_complete", False))
    return AgentTurnDecision.model_validate(data)


def _strip_json(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


class OpenRouterLanguageModel:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        retry_base_delay: float = 0.25,
        site_url: str | None = None,
        app_name: str | None = None,
        response_format: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        self.model = model or os.environ.get("OPENROUTER_MODEL")
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY is required for the openrouter provider")
        if not self.model:
            raise ValueError(
                "OPENROUTER_MODEL is required; set it to the exact OpenRouter model id"
            )
        self.base_url = (
            base_url or os.environ.get("OPENROUTER_BASE_URL", DEFAULT_BASE_URL)
        ).rstrip("/")
        self.timeout = timeout or float(
            os.environ.get("OPENROUTER_TIMEOUT_SECONDS", "60")
        )
        self.max_retries = (
            max_retries
            if max_retries is not None
            else _env_int("OPENROUTER_MAX_RETRIES", 1)
        )
        self.retry_base_delay = retry_base_delay
        self.response_format = (
            response_format
            if response_format is not None
            else os.environ.get("OPENROUTER_RESPONSE_FORMAT", "json_object")
        )
        self._site_url = site_url or os.environ.get("OPENROUTER_SITE_URL")
        self._app_name = app_name or os.environ.get("OPENROUTER_APP_NAME")
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url, timeout=self.timeout
        )

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=f"openrouter:{self.model}",
            kind="llm",
            local=False,
            notes="OpenAI-compatible chat completions; changing model is a config change.",
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if self._site_url:
            headers["HTTP-Referer"] = self._site_url
        if self._app_name:
            headers["X-Title"] = self._app_name
        return headers

    async def health(self) -> bool:
        try:
            response = await self._client.get(
                "/models", headers=self._headers(), timeout=min(self.timeout, 10.0)
            )
        except httpx.HTTPError:
            return False
        return response.status_code < 400

    async def decide(
        self,
        system_prompt: str,
        user_prompt: str,
        context: TurnContext,
    ) -> AgentTurnDecision:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        include_format = bool(
            self.response_format and self.response_format != "none"
        )
        format_fallback_used = False
        last_error = "OpenRouter request failed"
        for attempt in range(self.max_retries + 1):
            payload: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": 0.4,
            }
            if include_format:
                payload["response_format"] = {"type": self.response_format}
            try:
                response = await self._client.post(
                    "/chat/completions",
                    headers=self._headers(),
                    json=payload,
                    timeout=self.timeout,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = str(exc)
                if attempt < self.max_retries:
                    await asyncio.sleep(self.retry_base_delay * (2**attempt))
                    continue
                raise ProviderError(
                    "openrouter", f"transport failure: {exc}", retryable=True
                ) from exc

            if (
                response.status_code in {400, 422}
                and include_format
                and not format_fallback_used
            ):
                include_format = False
                format_fallback_used = True
                messages = [
                    *messages,
                    {
                        "role": "user",
                        "content": "Return only the requested JSON object, with no markdown.",
                    },
                ]
                continue

            if response.status_code >= 400:
                detail = response.text[:300]
                retryable = response.status_code in {408, 409, 429} or response.status_code >= 500
                last_error = f"HTTP {response.status_code}: {detail}"
                if retryable and attempt < self.max_retries:
                    await asyncio.sleep(self.retry_base_delay * (2**attempt))
                    continue
                raise ProviderError(
                    "openrouter",
                    last_error,
                    retryable=retryable,
                    status_code=response.status_code,
                )

            try:
                body = response.json()
                content = body["choices"][0]["message"]["content"]
                decision = _normalize_decision(json.loads(_strip_json(content)))
                return decision
            except (KeyError, IndexError, TypeError, json.JSONDecodeError, ValueError) as exc:
                last_error = f"invalid model JSON: {exc}"
                if attempt < self.max_retries:
                    messages = [
                        *messages,
                        {
                            "role": "user",
                            "content": "Return only the requested JSON object, with no markdown.",
                        },
                    ]
                    await asyncio.sleep(self.retry_base_delay * (2**attempt))
                    continue
                raise ProviderError(
                    "openrouter", last_error, retryable=False
                ) from exc

        raise ProviderError("openrouter", last_error, retryable=False)


__all__ = [
    "DEFAULT_BASE_URL",
    "OpenRouterLanguageModel",
    "OpenRouterSpeechToText",
    "OpenRouterTextToSpeech",
]


class OpenRouterSpeechToText:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        self.model = model or os.environ.get("OPENROUTER_STT_MODEL")
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY is required for the openrouter provider")
        if not self.model:
            raise ValueError("OPENROUTER_STT_MODEL is required for OpenRouter STT")
        self.base_url = (
            base_url or os.environ.get("OPENROUTER_BASE_URL", DEFAULT_BASE_URL)
        ).rstrip("/")
        self.timeout = timeout or float(
            os.environ.get("OPENROUTER_TIMEOUT_SECONDS", "60")
        )
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url, timeout=self.timeout
        )

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=f"openrouter-stt:{self.model}",
            kind="asr",
            local=False,
            notes="OpenRouter /audio/transcriptions; audio leaves the machine.",
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def health(self) -> bool:
        try:
            response = await self._client.get(
                "/models", headers=self._headers(), timeout=min(self.timeout, 10.0)
            )
        except httpx.HTTPError:
            return False
        return response.status_code < 400

    async def _transcribe(self, data: bytes, audio_format: str, language: str) -> str:
        import base64

        response = await self._client.post(
            "/audio/transcriptions",
            headers=self._headers(),
            json={
                "model": self.model,
                "input_audio": {
                    "data": base64.b64encode(data).decode("ascii"),
                    "format": audio_format,
                },
                "language": language,
                "response_format": "json",
            },
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise ProviderError(
                "openrouter-stt",
                f"HTTP {response.status_code}: {response.text[:300]}",
                retryable=response.status_code in {408, 429} or response.status_code >= 500,
                status_code=response.status_code,
            )
        try:
            return str(response.json().get("text", "")).strip()
        except ValueError as exc:
            raise ProviderError("openrouter-stt", f"invalid JSON: {exc}") from exc

    async def transcribe(
        self, pcm16_16khz: bytes, *, language: str = "en"
    ) -> Any:
        import io
        import wave

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16_000)
            writer.writeframes(pcm16_16khz)
        from .base import TranscriptResult

        text = await self._transcribe(buffer.getvalue(), "wav", language)
        return TranscriptResult(text=text, language=language)

    async def transcribe_media(self, data: bytes, suffix: str = ".ogg") -> str:
        audio_format = suffix.lstrip(".").lower() or "wav"
        return await self._transcribe(data, audio_format, "en")


class OpenRouterTextToSpeech:
    sample_rate = 22_050

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        voice: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        source_sample_rate: int | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        self.model = model or os.environ.get("OPENROUTER_TTS_MODEL")
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY is required for the openrouter provider")
        if not self.model:
            raise ValueError("OPENROUTER_TTS_MODEL is required for OpenRouter TTS")
        self.voice = voice or os.environ.get("OPENROUTER_TTS_VOICE")
        self.base_url = (
            base_url or os.environ.get("OPENROUTER_BASE_URL", DEFAULT_BASE_URL)
        ).rstrip("/")
        self.timeout = timeout or float(
            os.environ.get("OPENROUTER_TIMEOUT_SECONDS", "60")
        )
        self.source_sample_rate = source_sample_rate or _env_int(
            "OPENROUTER_TTS_SOURCE_SAMPLE_RATE", 24_000
        )
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url, timeout=self.timeout
        )

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=f"openrouter-tts:{self.model}",
            kind="tts",
            local=False,
            streaming_output=False,
            notes="OpenRouter /audio/speech; text leaves the machine.",
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def health(self) -> bool:
        try:
            response = await self._client.get(
                "/models", headers=self._headers(), timeout=min(self.timeout, 10.0)
            )
        except httpx.HTTPError:
            return False
        return response.status_code < 400

    async def synthesize(self, text: str) -> Any:
        import numpy as np

        if not text.strip():
            raise ProviderError("openrouter-tts", "text is empty")
        payload: dict[str, Any] = {
            "model": self.model,
            "input": text,
            "response_format": "pcm",
        }
        if self.voice:
            payload["voice"] = self.voice
        response = await self._client.post(
            "/audio/speech",
            headers=self._headers(),
            json=payload,
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise ProviderError(
                "openrouter-tts",
                f"HTTP {response.status_code}: {response.text[:300]}",
                retryable=response.status_code in {408, 429} or response.status_code >= 500,
                status_code=response.status_code,
            )
        data = response.content
        data = data[: len(data) - (len(data) % 2)]
        if self.source_sample_rate != self.sample_rate:
            samples = np.frombuffer(data, dtype="<i2")
            if samples.size:
                target_count = round(samples.size * self.sample_rate / self.source_sample_rate)
                positions = np.linspace(0, samples.size - 1, target_count)
                resampled = np.interp(positions, np.arange(samples.size), samples.astype(np.float64))
                data = np.clip(resampled, -32768, 32767).astype("<i2").tobytes()
        chunk_size = 8192
        for start in range(0, len(data), chunk_size):
            yield data[start : start + chunk_size]
