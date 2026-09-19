# PyInterviewBot

A voice-first AI talent assessment platform. The candidate hears a question,
answers by voice, and never sees a transcript, a score, or anything about
how they're being evaluated — they experience a conversation. Behind that,
the system silently transcribes, extracts evidence, updates a competency
graph, and decides how to adaptively probe deeper.

> **Status: working MVP slice, not a production product.** One demo
> scenario (a "Senior AI Solution Architect" RAG-architecture interview) runs
> genuinely end-to-end against real local models — nothing described below
> is a stub. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for exactly
> what's implemented vs. intentionally out of scope.

```
Traditional AI interviewer:  Ask → Answer → Score
This platform:                Listen → Understand → Probe → Verify → Assess
```

`ai-service` is also published on PyPI as
[`pyinterviewbot-ai`](https://pypi.org/project/pyinterviewbot-ai/) — but
`pip install pyinterviewbot-ai` alone gets you a FastAPI app hardcoded to
one demo scenario, still expecting `ollama serve` + `qwen2.5:7b-instruct`,
a separately-downloaded Piper voice, and `ffmpeg` on your PATH. It's not a
general-purpose library; publishing it just makes the code installable
without cloning the repo. Run it from this repo (see Setup below) unless
you specifically want that.

## Stack

- **Rust** (`gateway/`) — the real-time transport: WebSocket audio gateway,
  voice-activity detection, barge-in, and a deterministic, unit-tested
  session state machine. Never depends on LLM latency for correctness.
- **Python** (`ai-service/`) — ASR / LLM / TTS orchestration and the
  evidence engine. 100% local, open-source models — no API keys, nothing
  leaves your machine:
  - [faster-whisper](https://github.com/SYSTRAN/faster-whisper) for speech
    recognition
  - [Ollama](https://ollama.com) (`qwen2.5:7b-instruct`) for the interview
    agent
  - [Piper](https://github.com/OHF-Voice/piper1-gpl) for text-to-speech
- **JavaScript/TypeScript** (`candidate-client/`) — the candidate's voice
  UI (mic capture, playback, a deliberately blank "listening…" screen) and
  a `/recruiter` view where the transcript + evidence graph are actually
  visible.

## Prerequisites

- Rust (stable) + Cargo
- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/)
- Node.js + npm
- [Ollama](https://ollama.com) running locally, with `ollama pull qwen2.5:7b-instruct`
- `ffmpeg` on your PATH (used by the integration test script for resampling)

## Setup

```bash
# 1. Pull the LLM
ollama pull qwen2.5:7b-instruct

# 2. ai-service: install deps + download the Piper voice
cd ai-service
uv sync
mkdir -p models
uv run python -m piper.download_voices --download-dir models en_US-lessac-medium

# 3. gateway
cd ../gateway
cargo build --release

# 4. candidate-client
cd ../candidate-client
npm install
```

## Running the demo

Three processes, each in its own terminal:

```bash
# ai-service (port 8000)
cd ai-service && uv run uvicorn pyinterviewbot_ai.main:app --host 127.0.0.1 --port 8000

# gateway (port 8787)
cd gateway && RUST_LOG=info cargo run --release

# candidate-client (port 5173)
cd candidate-client && npm run dev
```

Then open `http://localhost:5173` — test your microphone, begin the
interview, and talk. Open `http://localhost:5173/recruiter` in another tab
to watch the transcript and evidence graph fill in as you go.

### Don't have a working browser mic handy? Run the real pipeline anyway

`ai-service/scripts/e2e_ws_test.py` drives the actual gateway WebSocket
protocol end-to-end using real synthesized audio (candidate answers are
generated with the same local Piper voice, resampled to 16kHz, and streamed
at real-time pace) — real VAD, real barge-in, real faster-whisper
transcription, real Ollama agent turns, real evidence extraction. Nothing
mocked. This is also what was used to find and fix the two timing bugs
documented in `docs/ARCHITECTURE.md`.

```bash
cd ai-service
uv run python scripts/e2e_ws_test.py
```

(Requires ai-service and gateway both already running, per above.)

## Repo layout

```
gateway/           Rust — audio gateway, VAD, session state machine
ai-service/         Python — ASR/LLM/TTS orchestration, evidence engine, SQLite
  scripts/          e2e_ws_test.py — full real-pipeline integration test
candidate-client/   Vite/TS — candidate voice UI + /recruiter view
docs/
  ARCHITECTURE.md   Layered design, what's real vs. simplified, bugs found
```

## License

Apache-2.0 — see [`LICENSE`](LICENSE).
