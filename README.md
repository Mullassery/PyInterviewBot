# PyInterviewBot

**A voice-first AI talent-assessment platform: the candidate hears a question, answers by voice, and experiences a conversation — behind the scenes the system transcribes, extracts evidence, and adapts.**

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/pyinterviewbot-ai)](https://pypi.org/project/pyinterviewbot-ai/)
[![CI](https://github.com/Mullassery/PyInterviewBot/actions/workflows/ci.yml/badge.svg)](https://github.com/Mullassery/PyInterviewBot/actions/workflows/ci.yml)
[![Graphify](https://img.shields.io/badge/Graphify-knowledge%20graph-blue)](https://github.com/Graphify-Labs/graphify)

## What it is

PyInterviewBot is a voice-first AI interview platform built from three services: a Rust
WebSocket audio gateway, a Python ASR/LLM/TTS orchestration and evidence engine, and a
TypeScript candidate/recruiter client. The candidate hears a question, answers by voice, and
never sees a transcript, a score, or anything about how they're being evaluated — they
experience a conversation. Behind that, the system silently transcribes, extracts evidence,
updates a competency graph, and decides how to adaptively probe deeper.

```
Traditional AI interviewer:  Ask → Answer → Score
This platform:                Listen → Understand → Probe → Verify → Assess
```

## Features

- **Voice-first candidate experience.** Mic capture, playback, and a deliberately blank
  "listening…" screen; no transcript or score is ever surfaced to the candidate.
- **Recruiter view** (`/recruiter`) where the transcript and evidence graph are actually
  visible as the interview runs.
- **Rust real-time gateway** (`gateway/`): WebSocket audio transport, voice-activity
  detection, barge-in, and a deterministic, unit-tested session state machine that never
  depends on LLM latency for correctness.
- **Local, open-source models only** (`ai-service/`): [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
  for speech recognition, [Ollama](https://ollama.com) (`qwen2.5:7b-instruct`) for the
  interview agent, and [Piper](https://github.com/OHF-Voice/piper1-gpl) for text-to-speech.
  No API keys; nothing leaves your machine.
- **End-to-end integration test** (`ai-service/scripts/e2e_ws_test.py`) that drives the real
  gateway protocol with real synthesized audio — real VAD, barge-in, transcription, agent
  turns, and evidence extraction, nothing mocked.

## Who it's for

- **Teams exploring voice-first AI interviewing** who want a genuinely end-to-end local
  reference implementation rather than a slide deck.
- **Engineers studying real-time voice pipelines** — the gateway/ai-service split shows how
  to keep transport correctness independent of model latency.
- **Anyone who wants an inspectable, no-cloud assessment loop** with a visible evidence
  graph.

## What it is not

- **Not a production product.** This is a working MVP slice with one demo scenario (a
  "Senior AI Solution Architect" RAG-architecture interview) that runs genuinely end-to-end
  against real local models. See [`ROADMAP_HONEST.md`](ROADMAP_HONEST.md) for exactly what's
  tested, what's built-but-unverified, and what isn't built yet.
- **Not remotely deployable as-is.** Per [`SECURITY.md`](SECURITY.md) there is no auth,
  CORS is a wildcard, and there is no TLS.
- **Not a general-purpose Python library.** `ai-service` is published on PyPI as
  [`pyinterviewbot-ai`](https://pypi.org/project/pyinterviewbot-ai/), but installing it
  alone gets you a FastAPI app hardcoded to the one demo scenario, still expecting
  `ollama serve` + `qwen2.5:7b-instruct`, a separately-downloaded Piper voice, and `ffmpeg`
  on your PATH. Run it from this repo unless you specifically want that.
- **No automated Python/E2E tests in CI yet.** `ai-service` has zero automated tests today;
  the real integration test needs Ollama and a downloaded Piper voice, so it is run locally,
  not in CI.

## Project status

**Working MVP / early prototype.** Version 0.1.0. The single demo scenario is real and
runs end-to-end; everything beyond it should be treated as unverified. See
[`ROADMAP_HONEST.md`](ROADMAP_HONEST.md) and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
Changes are tracked in [CHANGELOG.md](CHANGELOG.md).

## Installation

Prerequisites:

- Rust (stable) + Cargo
- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/)
- Node.js + npm
- [Ollama](https://ollama.com) running locally, with `ollama pull qwen2.5:7b-instruct`
- `ffmpeg` on your PATH (used by the integration test script for resampling)

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

## Quick start

Three processes, each in its own terminal:

```bash
# ai-service (port 8000)
cd ai-service && uv run uvicorn pyinterviewbot_ai.main:app --host 127.0.0.1 --port 8000

# gateway (port 8787)
cd gateway && RUST_LOG=info cargo run --release

# candidate-client (port 5173)
cd candidate-client && npm run dev
```

Then open `http://localhost:5173` — test your microphone, begin the interview, and talk.
Open `http://localhost:5173/recruiter` in another tab to watch the transcript and evidence
graph fill in as you go.

## Usage

**Don't have a working browser mic handy? Run the real pipeline anyway.**
`ai-service/scripts/e2e_ws_test.py` drives the actual gateway WebSocket protocol
end-to-end using real synthesized audio (candidate answers are generated with the same
local Piper voice, resampled to 16kHz, and streamed at real-time pace) — real VAD, real
barge-in, real faster-whisper transcription, real Ollama agent turns, real evidence
extraction. Nothing mocked.

```bash
cd ai-service
uv run python scripts/e2e_ws_test.py
```

(Requires ai-service and gateway both already running, per above.)

### Repository layout

```
gateway/            Rust — audio gateway, VAD, session state machine
ai-service/         Python — ASR/LLM/TTS orchestration, evidence engine, SQLite
  scripts/          e2e_ws_test.py — full real-pipeline integration test
candidate-client/   Vite/TS — candidate voice UI + /recruiter view
docs/
  ARCHITECTURE.md   Layered design, what's real vs. simplified, bugs found
```

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — layered design, what's real vs. simplified,
  and the two timing bugs found and fixed while building the integration test.
- [ROADMAP_HONEST.md](ROADMAP_HONEST.md) — exactly what's tested, built-but-unverified, and
  not built.
- [CHANGELOG.md](CHANGELOG.md) — release history.
- [SECURITY.md](SECURITY.md) — the current, concrete security gaps.

## Known issues and deferred work

**Blocked / failed:**
- Auth/TLS, multi-scenario config, consent/retention, bias evaluation, CI E2E.

**Rejected / deferred:**
- Production hiring without compliance review.

Full shipped/blocked/rejected detail: [`QUEUE_STATUS.md`](QUEUE_STATUS.md).

## Queue status

Shipped, blocked/failed, and rejected work for this repository is documented in
[`QUEUE_STATUS.md`](QUEUE_STATUS.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to set up and test changes, and
[SECURITY.md](SECURITY.md) for the current security gaps. CI runs `cargo fmt`/`clippy`/`test`
for the gateway, `ruff` + an import sanity check for `ai-service`, and `tsc` + `npm run
build` for the candidate client.

## Related projects

Other tools from the same family, for adjacent parts of the AI stack:

- [PyInferenceManager](https://github.com/Mullassery/PyInferenceManager) — multi-provider LLM inference orchestrator with cost/latency-aware routing and budget enforcement.
- [PyAgentHound](https://github.com/Mullassery/PyAgentHound) — debug AI systems by tracing execution, detecting failures, identifying root causes, and explaining bad agent results.
- [PyStreamAI](https://github.com/Mullassery/PyStreamAI) — ML deployment toolkit: canary/A-B routing, cost tracking, request scheduling, hot model reload, and ONNX Runtime inference.
- [PyStreamMCP](https://github.com/Mullassery/PyStreamMCP) — intelligence layer for AI agents: query planning, context discovery, and cost optimization.
- [PyTokenCalc](https://github.com/Mullassery/PyTokenCalc) — count tokens and estimate LLM costs across 20+ providers before you send a request.

[Browse all projects →](https://github.com/Mullassery?tab=repositories)

## License

Apache-2.0 — see [`LICENSE`](LICENSE).
