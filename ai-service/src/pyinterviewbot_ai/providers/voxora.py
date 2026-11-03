"""Voxora-backed language model - the redirected LLM provider (ADR-3D-LLM).

OpenRouter connectivity, model routing and billing live in Voxora Studio;
this provider forwards one-turn decision prompts to Voxora's
``POST /api/v1/ai/decide`` and keeps the interview methodology local.
Fail-closed: Voxora is required to be configured (VOXORA_BASE_URL +
VOXORA_API_KEY) or the provider registry refuses to build.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..voxora_client import VoxoraClient, VoxoraClientError, VoxoraConfigError
from .base import AgentTurnDecision, ProviderCapabilities, ProviderError

if TYPE_CHECKING:  # pragma: no cover
    from .base import TurnContext

logger = logging.getLogger(__name__)

_NEXT_ACTIONS = {"probe", "clarify", "challenge", "move_on", "simulate", "conclude"}
_EVIDENCE_STATUSES = {"confirmed", "weak", "contradicted", "not_addressed"}


def normalize_decision(payload: Any) -> AgentTurnDecision:
    """Normalize Voxora/OpenRouter-shape model output to AgentTurnDecision."""
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
        logger.warning("normalizing unknown next_action %r", action)
        action = "conclude" if data.get("is_complete") else "probe"
    data["next_action"] = action
    data["question_text"] = str(
        data.get("question_text", data.get("question", data.get("text", "")))
    )
    data["is_complete"] = bool(data.get("is_complete", False))
    return AgentTurnDecision.model_validate(data)


class VoxoraLanguageModel:
    """LanguageModel over the Voxora decide contract."""

    def __init__(self, client: VoxoraClient | None = None) -> None:
        """Allow tests to inject a fake-transport client."""
        self._client = client or VoxoraClient()
        self._model_profile = self._pick_profile()

    @classmethod
    def _pick_profile(cls) -> str:
        import os

        return os.environ.get("PYINTERVIEWBOT_VOXORA_MODEL_PROFILE", "interview_default")

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=f"voxora ({self._model_profile})",
            kind="llm",
            local=False,
            streaming_input=False,
            streaming_output=False,
            languages=("en",),
            notes="Model requests route through Voxora's /api/v1/ai/decide "
            "(keys + routing + billing live in Voxora Studio).",
        )

    async def health(self) -> bool:
        try:
            await self._client_health()
            return True
        except (VoxoraClientError, VoxoraConfigError, OSError):
            return False

    async def _client_health(self) -> None:
        import asyncio

        await asyncio.to_thread(self._client.voice_session_transcript, "@health-probe")

    async def decide(
        self,
        system_prompt: str,
        user_prompt: str,
        context: TurnContext,
    ) -> AgentTurnDecision:
        import asyncio

        try:
            decision = await asyncio.to_thread(
                self._client.decide,
                system_prompt,
                user_prompt,
                model_profile=self._model_profile,
            )
        except VoxoraConfigError as exc:
            raise ProviderError("voxora", str(exc), retryable=False) from None
        except VoxoraClientError as exc:
            raise ProviderError("voxora", str(exc), retryable=exc.retryable) from exc
        try:
            return normalize_decision(decision)
        except (TypeError, ValueError) as error:
            raise ProviderError(
                "voxora",
                f"invalid decision payload: {error}",
                retryable=False,
            ) from None


__all__ = ["VoxoraLanguageModel", "normalize_decision"]
