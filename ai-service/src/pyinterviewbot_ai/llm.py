"""LLM provider (spec §31: interchangeable behind a narrow interface).
Only implementation right now is local Ollama; ASR/TTS have their own
similarly narrow wrappers in asr.py/tts.py. Nothing outside this module
imports `ollama` directly.
"""

from __future__ import annotations

import json
import os

import ollama

from .models import AgentTurnDecision

MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b-instruct")

_client = ollama.AsyncClient()


async def decide_turn(system_prompt: str, user_prompt: str) -> AgentTurnDecision:
    response = await _client.chat(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        format=AgentTurnDecision.model_json_schema(),
        options={"temperature": 0.4},
    )
    content = response.message.content
    return AgentTurnDecision.model_validate(json.loads(content))
