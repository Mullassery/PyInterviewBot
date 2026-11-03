"""Voxora Studio client — the only channel to telephony/LLM infrastructure.

Versioned client for the ecosystem ADR-3 contracts:
- POST /api/v1/ai/decide                    (ADR-3D-LLM)
- POST /api/v1/voice-sessions               (ADR-3B)
- GET  /api/v1/voice-sessions/{session_id}
- POST /api/v1/voice-sessions/{session_id}/terminate
- GET  /api/v1/voice-sessions/{session_id}/transcript
- POST /api/v1/voice-sessions/{session_id}/instructions

PyInterviewBot keeps interview methodology; Voxora owns the voice session,
the telephony provider, and the model router. Server code never imports
another repository's modules; this client talks HTTP only.
"""

from __future__ import annotations

import os
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover - httpx ships with dev deps
    httpx = None  # type: ignore[assignment]

VOXORA_CONTRACT_VERSION = "1.0"


class VoxoraConfigError(Exception):
    """The Voxora integration is misconfigured (fail closed with a pointer)."""


class VoxoraClientError(Exception):
    """Voxora call failed after retries / contract mismatch."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        """Tag failures with retry semantics for upstream orchestration."""
        super().__init__(message)
        self.retryable = retryable


class VoxoraClient:
    provider = "voxora"

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        client: Any | None = None,
    ) -> None:
        """Build config env-first, failing closed with explicit overrides."""
        source = os.environ
        self.base_url = (base_url or source.get("VOXORA_BASE_URL") or "").rstrip("/")
        if not self.base_url:
            raise VoxoraConfigError(
                "VOXORA_BASE_URL is required to reach Voxora Studio "
                "(or set PYINTERVIEWBOT_*_PROVIDER to local/mock for standalone modes)"
            )
        self.api_key = api_key or source.get("VOXORA_API_KEY") or ""
        if not self.api_key:
            raise VoxoraConfigError(
                "VOXORA_API_KEY is required (service-scoped credential, never shared)"
            )
        self.timeout = timeout_seconds if timeout_seconds is not None else float(
            source.get("VOXORA_TIMEOUT_SECONDS", "30")
        )
        self.max_retries = max_retries if max_retries is not None else int(
            source.get("VOXORA_MAX_RETRIES", "2")
        )
        self._client = client if client is not None else httpx.Client(  # type: ignore[union-attr]
            base_url=self.base_url, timeout=self.timeout
        )

    # ------------------------------------------------------------ internals
    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, payload)

    def _get(self, path: str) -> dict[str, Any]:
        return self._request("GET", path, None)

    def _request(self, method: str, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        import time

        retryable_statuses = {408, 429, 500, 502, 503, 504}
        # Join the base explicitly so injected/self-built clients both get
        # scheme-aware URLs (cookie handling breaks on scheme-less paths).
        url = f"{self.base_url}{path}" if self.base_url.startswith("http") else path
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "X-Voxora-Application": "pyinterviewbot",
        }
        last_error = VoxoraClientError("voxora request failed", retryable=True)
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.request(
                    method, url, json=payload, headers=headers
                )
            except Exception as error:  # transport-level
                last_error = VoxoraClientError(
                    f"voxora_transport_failure: {type(error).__name__}: {error}"[:300],
                    retryable=True,
                )
                if attempt < self.max_retries:
                    time.sleep(min(0.25 * (2**attempt), 2.0))
                    continue
                raise last_error from error
            if response.status_code in retryable_statuses:
                last_error = VoxoraClientError(
                    f"voxora_http_{response.status_code}", retryable=True
                )
                if attempt < self.max_retries:
                    retry_after = response.headers.get("retry-after")
                    try:
                        wait = float(retry_after) if retry_after else None
                    except (TypeError, ValueError):
                        wait = None
                    time.sleep(wait or min(0.25 * (2**attempt), 2.0))
                    continue
                raise last_error from None
            if response.status_code >= 400:
                raise VoxoraClientError(
                    f"voxora_http_{response.status_code}: {response.text[:300]}",
                    retryable=False,
                )
            try:
                body = response.json()
            except ValueError as error:
                raise VoxoraClientError("voxora returned non-JSON body") from error
            if not isinstance(body, dict):
                raise VoxoraClientError("voxora returned a non-object body")
            return body
        raise last_error

    # ---------------------------------------------------------- agentic LLM
    def decide(
        self, system_prompt: str, user_prompt: str, *, model_profile: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
        }
        if model_profile:
            payload["model_profile"] = model_profile
        body = self._post("/api/v1/ai/decide", payload)
        decision = body.get("decision")
        if not isinstance(decision, dict):
            raise VoxoraClientError("voxora decide response missing 'decision' object")
        return decision

    # -------------------------------------------------------- voice sessions
    def create_voice_session(
        self,
        *,
        external_application: str,
        external_session_id: str,
        contact_external_id: str,
        contact_name: str,
        phone: str,
        agent_id: str,
        model_profile: str | None = None,
        session_metadata: dict[str, Any] | None = None,
        webhook_url: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "external_application": external_application,
            "external_session_id": external_session_id,
            "contact": {
                "external_contact_id": contact_external_id,
                "name": contact_name,
                "phone": phone,
            },
            "agent": {"agent_id": agent_id, "model_profile": model_profile},
            "session_metadata": session_metadata or {},
        }
        if webhook_url:
            payload["webhook_url"] = webhook_url
        return self._post("/api/v1/voice-sessions", payload)

    def get_voice_session(self, session_id: str) -> dict[str, Any]:
        return self._get(f"/api/v1/voice-sessions/{session_id}")

    def terminate_voice_session(self, session_id: str) -> dict[str, Any]:
        return self._post(f"/api/v1/voice-sessions/{session_id}/terminate", {})

    def voice_session_transcript(self, session_id: str) -> dict[str, Any]:
        return self._get(f"/api/v1/voice-sessions/{session_id}/transcript")

    def session_instruction(
        self, session_id: str, instruction_id: str, text: str, *, type_: str = "speak"
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "instruction_id": instruction_id,
            "type": type_,
            "text": text,
        }
        return self._post(f"/api/v1/voice-sessions/{session_id}/instructions", payload)

    def health(self) -> bool:
        body = self._get("/api/v1/health")
        return bool(body.get("status") == "ok" or body.get("status") == "degraded")
