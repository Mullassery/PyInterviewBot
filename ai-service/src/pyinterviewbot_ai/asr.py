"""ASR provider (spec §31): local faster-whisper. Per-turn transcription of
a complete VAD-delimited utterance — not token-level streaming partials.
See docs/ARCHITECTURE.md for why that's an intentional MVP simplification
of the §11/§27 streaming pipeline (faster-whisper has no native incremental
decode mode; a real streaming-partials setup would periodically re-decode a
growing buffer, which added complexity this slice skips since nothing
candidate-facing depends on partial transcripts anyway).
"""

from __future__ import annotations

import asyncio
import os

import numpy as np
from faster_whisper import WhisperModel

MODEL_SIZE = os.environ.get("WHISPER_MODEL", "small.en")
DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")

_model: WhisperModel | None = None


def _get_model() -> WhisperModel:
    global _model
    if _model is None:
        _model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE)
    return _model


def _pcm16_bytes_to_float32(pcm16: bytes) -> np.ndarray:
    samples = np.frombuffer(pcm16, dtype="<i2")
    return samples.astype(np.float32) / 32768.0


def _transcribe_sync(pcm16_16khz: bytes) -> str:
    audio = _pcm16_bytes_to_float32(pcm16_16khz)
    if audio.size == 0:
        return ""
    segments, _info = _get_model().transcribe(audio, language="en", vad_filter=False)
    return " ".join(segment.text.strip() for segment in segments).strip()


async def transcribe(pcm16_16khz: bytes) -> str:
    # faster-whisper is synchronous/CPU-bound; keep it off the event loop.
    return await asyncio.to_thread(_transcribe_sync, pcm16_16khz)
