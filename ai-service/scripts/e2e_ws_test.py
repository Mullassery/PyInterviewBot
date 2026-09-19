"""Live end-to-end integration test against the real gateway + ai-service
processes (both must already be running — see README).

This isn't a human speaking into a microphone, but it IS the real audio
pipeline: candidate answers are synthesized with the same local Piper voice
the AI uses, resampled to 16kHz, and streamed to the gateway's actual
WebSocket protocol exactly as the browser client would — real VAD, real
barge-in detection, real faster-whisper transcription, real Ollama agent
turns, real evidence extraction. Nothing here is mocked.

Usage: uv run --project ai-service python ai-service/scripts/e2e_ws_test.py
"""

from __future__ import annotations

import asyncio
import json
import subprocess

import httpx
import websockets

GATEWAY_WS = "ws://127.0.0.1:8787/ws/interview"
AI_SERVICE = "http://127.0.0.1:8000"

ANSWERS = [
    "We used Azure AI Search for vector retrieval over our document corpus, and Azure "
    "OpenAI GPT-4 for generation. Azure AI Search handled semantic search over embeddings, "
    "and the model just used whatever top-k chunks came back to generate the answer.",
    "We used k equals five initially based on some offline evaluation. When retrieval "
    "returned irrelevant documents, honestly the model would sometimes just hallucinate an "
    "answer anyway using the irrelevant context, which was a real problem for us in production.",
    "We added a relevance threshold on the search score, so if the top result was below a "
    "cutoff we would tell the model there was no relevant context instead of forcing an "
    "answer. That cut down hallucinations a lot.",
    "First I would check A K S pod autoscaling and CPU and memory on the retrieval and "
    "generation services. If that was fine I would look at Azure AI Search throughput limits "
    "and whether we were getting rate limited, then check Azure OpenAI token per minute quota "
    "since that is usually the real bottleneck at that scale.",
    "We would watch pod CPU and memory utilization and the Kubernetes autoscaler events, and "
    "monitor Azure Monitor for throttled search requests and latency percentiles.",
]

BARGE_IN_PHRASE = "Sorry, can I clarify something first?"
FILLER_ANSWER = "I think that covers my approach, thank you."


async def synthesize_16k(text: str) -> bytes:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(f"{AI_SERVICE}/tts/synthesize", json={"text": text})
        resp.raise_for_status()
        pcm22k = resp.content

    proc = subprocess.run(
        [
            "ffmpeg", "-f", "s16le", "-ar", "22050", "-ac", "1", "-i", "pipe:0",
            "-f", "s16le", "-ar", "16000", "-ac", "1", "pipe:1",
        ],
        input=pcm22k,
        capture_output=True,
        check=True,
    )
    return proc.stdout


def trailing_silence(ms: int) -> bytes:
    return b"\x00\x00" * (16 * ms)


async def stream_audio(ws, pcm16: bytes) -> None:
    """Sends audio at real-time pace (20ms chunk every 20ms), exactly like a
    real mic stream would arrive. This matters: blasting everything at once
    desyncs the gateway's VAD timing (200ms speech-confirm / 700ms
    silence-to-end-turn) from wall-clock reality and desyncs this script's
    turn-taking from the gateway's actual state.
    """
    chunk_bytes = 16 * 20 * 2  # 20ms @ 16kHz mono 16-bit
    full = pcm16 + trailing_silence(900)  # trailing silence forces VAD end-of-turn
    for i in range(0, len(full), chunk_bytes):
        await ws.send(full[i : i + chunk_bytes])
        await asyncio.sleep(0.02)


async def run() -> None:
    print("Synthesizing candidate audio locally (Piper)...")
    answer_audio = [await synthesize_16k(a) for a in ANSWERS]
    filler_audio = await synthesize_16k(FILLER_ANSWER)
    barge_in_audio = await synthesize_16k(BARGE_IN_PHRASE)
    print("Done.\n")

    turn_idx = 0
    barge_in_attempted = False
    got_interrupt = False
    just_interrupted = False
    send_task: asyncio.Task | None = None

    async with websockets.connect(GATEWAY_WS, max_size=None) as ws:
        while True:
            msg = await ws.recv()
            if isinstance(msg, (bytes, bytearray)):
                continue  # TTS audio chunk — not needed for this test

            data = json.loads(msg)

            if data["type"] == "error":
                print(f"[ERROR] {data['message']}")
                continue
            if data["type"] == "interrupt":
                got_interrupt = True
                just_interrupted = True
                print("    <<< interrupt acknowledged by gateway >>>")
                continue

            state = data["state"]
            text = data.get("text")
            if text:
                print(f"\n[turn {data['turn']}] AI: {text}")

            # Sends run as background tasks so this loop keeps reading
            # gateway messages (state changes, interrupts) the whole time a
            # send is in flight — exactly like a real browser client, where
            # mic capture and the WS receive handler run concurrently. A
            # blocking `await stream_audio(...)` here would let several
            # gateway messages queue up unread and get handled in the wrong
            # order relative to what this script sends next.
            if state == "ai_speaking" and not barge_in_attempted:
                barge_in_attempted = True
                await asyncio.sleep(0.3)  # let a little TTS audio actually stream first
                print("    >>> barging in mid-question...")
                send_task = asyncio.create_task(stream_audio(ws, barge_in_audio))

            elif state == "listening":
                if just_interrupted:
                    # This listening state just means "continue the
                    # utterance you already started" (the barge-in phrase,
                    # already in flight) — not a fresh turn to answer.
                    just_interrupted = False
                    print("    (post-interrupt listening — barge-in utterance in flight)")
                elif turn_idx < len(answer_audio):
                    print(f"    >>> candidate answers (turn {turn_idx + 1})...")
                    send_task = asyncio.create_task(stream_audio(ws, answer_audio[turn_idx]))
                    turn_idx += 1
                else:
                    send_task = asyncio.create_task(stream_audio(ws, filler_audio))

            elif state == "completed":
                print("\n=== interview completed ===")
                if send_task is not None and not send_task.done():
                    send_task.cancel()
                break

    assert got_interrupt, "barge-in was never acknowledged — TTS did not stop on interruption"
    print("\nBarge-in: OK (TTS stopped immediately on candidate speech)")
    print(f"Turns answered from canned audio: {turn_idx}/{len(answer_audio)}")


async def main() -> None:
    try:
        await asyncio.wait_for(run(), timeout=240)
    except asyncio.TimeoutError:
        print("\nTIMED OUT — session never reached 'completed'. Check gateway/ai-service logs.")
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
