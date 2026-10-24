"""OpenRouter OpenAI-compatible chat-completions provider.

The model is explicit (`OPENROUTER_MODEL`) so changing models is a deliberate
configuration change. API keys stay server-side; no key is logged or returned.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx

from ..models import AgentTurnDecision
from .base import ProviderCapabilities, ProviderError, TurnContext

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


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
                decision = AgentTurnDecision.model_validate(json.loads(_strip_json(content)))
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


__all__ = ["DEFAULT_BASE_URL", "OpenRouterLanguageModel"]
