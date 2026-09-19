"""TTS provider (spec §31): local Piper. Synthesizes and streams raw
PCM16LE mono audio chunk-by-chunk as they're produced, rather than
buffering the whole utterance — this is what lets the gateway start
forwarding audio to the browser before the sentence has finished
synthesizing (spec §27).
"""

from __future__ import annotations

import asyncio
import os
import queue
import threading
from collections.abc import AsyncIterator

from piper.voice import PiperVoice

MODEL_PATH = os.environ.get("PIPER_MODEL_PATH", "models/en_US-lessac-medium.onnx")

_voice: PiperVoice | None = None
_lock = threading.Lock()

SAMPLE_RATE = 22050  # must match the loaded voice's config; asserted at load time


def _get_voice() -> PiperVoice:
    global _voice
    with _lock:
        if _voice is None:
            voice = PiperVoice.load(MODEL_PATH)
            assert voice.config.sample_rate == SAMPLE_RATE, (
                f"voice sample rate {voice.config.sample_rate} != expected {SAMPLE_RATE}; "
                "update tts.SAMPLE_RATE and the candidate client's playback rate together"
            )
            _voice = voice
    return _voice


_SENTINEL = object()


def _synthesize_worker(text: str, out_queue: "queue.Queue[object]") -> None:
    try:
        voice = _get_voice()
        for chunk in voice.synthesize(text):
            out_queue.put(chunk.audio_int16_bytes)
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller via the queue
        out_queue.put(exc)
    finally:
        out_queue.put(_SENTINEL)


async def synthesize_stream(text: str) -> AsyncIterator[bytes]:
    out_queue: "queue.Queue[object]" = queue.Queue()
    thread = threading.Thread(target=_synthesize_worker, args=(text, out_queue), daemon=True)
    thread.start()

    loop = asyncio.get_event_loop()
    while True:
        item = await loop.run_in_executor(None, out_queue.get)
        if item is _SENTINEL:
            break
        if isinstance(item, Exception):
            raise item
        yield item  # type: ignore[misc]
