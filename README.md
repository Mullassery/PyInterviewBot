# PyInterviewBot

**A voice-first AI talent-assessment platform: the candidate hears a question, answers by voice, and experiences a conversation — behind the scenes the system transcribes, extracts evidence, and adapts.**

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/pyinterviewbot-ai)](https://pypi.org/project/pyinterviewbot-ai/)
[![CI](https://github.com/Mullassery/PyInterviewBot/actions/workflows/ci.yml/badge.svg)](https://github.com/Mullassery/PyInterviewBot/actions/workflows/ci.yml)

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
- **Local, open-source models by default** (`ai-service/`): [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
  for speech recognition, [Ollama](https://ollama.com) (`qwen2.5:7b-instruct`) for the
  interview agent, and [Piper](https://github.com/OHF-Voice/piper1-gpl) for text-to-speech.
  No API keys; nothing leaves your machine.
- **Provider-neutral speech interfaces.** ASR/TTS/LLM are selected by environment
  (`local` or `mock`) behind narrow contracts. Interview state and evidence stay in
  PyInterviewBot; a provider is never the source of truth.
- **Self-hosted Rust voice runtime** (`voice-runtime/`): session lifecycle, sequenced
  normalized events, bounded audio, cancellation, idle timeouts, capacity limits, and a
  deterministic mock provider, all in Rust with unit tests.
- **Deterministic mock mode.** `PYINTERVIEWBOT_PROVIDER=mock` runs a complete interview,
  report, and review flow with no models, network, or paid API. CI uses this path.
- **Templates and evidence-linked reports.** Role-specific templates define explicit
  competencies, question banks, and turn limits. `GET /sessions/{id}/report` links every
  rubric node to transcript quotes, flags model notes as inference, exposes uncertainty,
  and requires human review.
- **Bounded real-time transport.** Configurable VAD silence, per-turn audio caps,
  WebSocket frame/message limits, ai-service auth forwarding, playback-duration-aware TTS,
  barge-in, and `/ready` dependency health checks.
- **Native managed-call workflow.** The Rust runtime includes server-side call creation,
  webhook authentication, idempotent event normalization, and provider event queuing for
  operators who intentionally use a managed call provider. It is never required for the
  local path and does not own interview state. See
  [`docs/CAPABILITY_MATRIX.md`](docs/CAPABILITY_MATRIX.md).
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

- **Not a production product.** This is a working MVP with three built-in templates and a
  real local voice path. See [`docs/internal/ROADMAP_HONEST.md`](docs/internal/ROADMAP_HONEST.md)
  and [`docs/internal/BASELINE_AUDIT.md`](docs/internal/BASELINE_AUDIT.md) for what is
  verified versus still open.
- **Not remotely deployable as-is.** Auth exists, but there is no TLS, no per-session
  authorization, and no rate limiting. See [`SECURITY.md`](SECURITY.md) and
  [`docs/SECURITY_MODEL.md`](docs/SECURITY_MODEL.md).
- **Not a general-purpose Python library.** `ai-service` is published on PyPI as
  [`pyinterviewbot-ai`](https://pypi.org/project/pyinterviewbot-ai/), but installing it
  alone still expects local model configuration. Run it from this repo unless you
  specifically want that.
- **No live E2E in CI.** CI runs deterministic mock-provider tests; the real integration
  test needs Ollama and a downloaded Piper voice, so it is run locally.

## Project status

**Working MVP / early prototype.** Version 0.1.1 plus the 2026-10-25 provider/report
hardening. The default local path, deterministic mock path, and Rust voice runtime are
tested; the optional managed-call provider path remains unverified. See
[`docs/internal/ROADMAP_HONEST.md`](docs/internal/ROADMAP_HONEST.md) and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
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

### Gateway authentication

The Rust WebSocket gateway now requires a token when `GATEWAY_API_KEY` is set:

```bash
GATEWAY_API_KEY=change-me cargo run -p gateway
# ws://127.0.0.1:8787/ws/interview?token=change-me
# or Authorization: Bearer change-me
```

If unset, the gateway is local-development mode; bind it to loopback and do
not expose it. `/health` stays unauthenticated; `/ready` checks ai-service.

When ai-service requires a key, set the same value for the gateway as
`AI_SERVICE_API_KEY` (or `PYINTERVIEWBOT_API_KEY`) so it can forward it:

```bash
PYINTERVIEWBOT_API_KEY=change-me GATEWAY_API_KEY=gw-secret cargo run -p gateway
```

## Authentication and retention

Every non-health ai-service endpoint requires authentication:

- Set `PYINTERVIEWBOT_API_KEY` and send `X-API-Key: ...` or
  `Authorization: Bearer ...`.
- If the variable is unset, only loopback clients are accepted; a
  network-exposed service fails closed instead of serving transcripts to
  anyone who can reach the port.
- `DELETE /sessions/{id}` deletes the session, transcript, evidence, and review
  rows for a retention request.
- `POST /sessions` returns a per-session `session_token` (stored as a SHA-256
  hash). The gateway forwards it on `/turn`. Set
  `PYINTERVIEWBOT_REQUIRE_SESSION_TOKEN=1` to reject turns that lack it, even
  when the caller has the global API key.
- `PYINTERVIEWBOT_RATE_LIMIT` (requests per `PYINTERVIEWBOT_RATE_WINDOW_SECONDS`,
  default 120/60s) applies per API key or client IP. `0` disables the
  in-process limiter; use a shared proxy limit for multi-worker deployments.

The gateway forwards its ai-service key automatically. `/health` remains
unauthenticated for probes. Session tokens are defense-in-depth, not a
replacement for scoped/tenant authorization; see
[`docs/SECURITY_MODEL.md`](docs/SECURITY_MODEL.md).

## Provider selection and mock mode

```bash
# Fully offline deterministic flow: no Ollama, Piper voice, or Whisper weights
PYINTERVIEWBOT_PROVIDER=mock \
  uv run uvicorn pyinterviewbot_ai.main:app --host 127.0.0.1 --port 8000

# Or select providers individually
PYINTERVIEWBOT_ASR_PROVIDER=local
PYINTERVIEWBOT_TTS_PROVIDER=local
PYINTERVIEWBOT_LLM_PROVIDER=local

```

`GET /providers` (authenticated) reports configured providers, capabilities,
and health. The self-hosted Rust voice runtime lives in `voice-runtime/` and is
built/tested with the workspace. The managed-call workflow is optional and is
never required—see [`docs/CAPABILITY_MATRIX.md`](docs/CAPABILITY_MATRIX.md).

## Native Rust voice endpoints

The gateway exposes the in-repo Rust speech studio and optional managed-call
workflow directly (same gateway auth: Bearer token or `?token=`):

```bash
# Studio lifecycle and speech
curl -H "Authorization: Bearer $GATEWAY_API_KEY" \
  http://127.0.0.1:8787/native/voice/health
curl -H "Authorization: Bearer $GATEWAY_API_KEY" \
  http://127.0.0.1:8787/native/voice/capabilities
curl -H "Authorization: Bearer $GATEWAY_API_KEY" \
  http://127.0.0.1:8787/native/voice/engines
SID=$(curl -s -X POST -H "Authorization: Bearer $GATEWAY_API_KEY" \
  http://127.0.0.1:8787/native/voice/sessions | jq -r .session_id)
curl -H "Authorization: Bearer $GATEWAY_API_KEY" -H 'content-type: application/json' \
  -X POST http://127.0.0.1:8787/native/voice/sessions/$SID/speak \
  -d '{"text":"Tell me about the system."}' --output speech.pcm
curl -H "Authorization: Bearer $GATEWAY_API_KEY" -H 'content-type: application/json' \
  -X POST http://127.0.0.1:8787/native/voice/sessions/$SID/speak/stream \
  -d '{"text":"Tell me about the system."}' --output speech-stream.pcm
curl -H "Authorization: Bearer $GATEWAY_API_KEY" -H 'content-type: application/octet-stream' \
  -X POST 'http://127.0.0.1:8787/native/voice/sessions/'$SID'/transcribe?sample_rate=16000' \
  --data-binary @answer.pcm
# Explicit recording (never automatic; in-memory until cleared/session close)
curl -H "Authorization: Bearer $GATEWAY_API_KEY" -H 'content-type: application/octet-stream' \
  -X POST 'http://127.0.0.1:8787/native/voice/sessions/'$SID'/record?sample_rate=16000' \
  --data-binary @answer.pcm
curl -H "Authorization: Bearer $GATEWAY_API_KEY" \
  http://127.0.0.1:8787/native/voice/sessions/$SID/recording.wav --output recording.wav
curl -X DELETE -H "Authorization: Bearer $GATEWAY_API_KEY" \
  http://127.0.0.1:8787/native/voice/sessions/$SID/recording

curl -X DELETE -H "Authorization: Bearer $GATEWAY_API_KEY" \
  http://127.0.0.1:8787/native/voice/sessions/$SID

# Optional managed-call workflow (server-only credentials)
curl -H "Authorization: Bearer $GATEWAY_API_KEY" \
  http://127.0.0.1:8787/native/managed-call/status
```

The default speech engine is the deterministic mock (`/native/voice/capabilities`
reports `engine: "mock"`). Real local inference plugs into the `SpeechEngine`
trait without changing the runtime, gateway, or interview domain. Configure the
optional managed-call workflow with `MANAGED_CALL_API_KEY`,
`MANAGED_CALL_BASE_URL`, and `MANAGED_CALL_WEBHOOK_SECRET` (plus
`MANAGED_CALL_WEBHOOK_AUTH_MODE=bearer|hmac`); no provider account is required
for the local path. Webhook bodies are size-limited, authenticated in constant
time, and de-duplicated before normalization. Transport retries use
`MANAGED_CALL_MAX_RETRIES` (default 2) and `MANAGED_CALL_RETRY_BASE_MS`
(default 200) with exponential backoff for retryable failures.

`/native/voice/engines` lists registered speech engines, default selection, and
health. The default engine remains the deterministic mock until a real local
engine is registered.

## Templates, reports, and human review

```bash
# Start a specific template (default: rag_architect)
curl -X POST http://127.0.0.1:8000/sessions \
  -H "X-API-Key: change-me" -H "Content-Type: application/json" \
  -d '{"template_id":"backend_engineer"}'

# Evidence-linked report and append-only human correction
curl http://127.0.0.1:8000/sessions/<id>/report -H "X-API-Key: change-me"
curl -X POST http://127.0.0.1:8000/sessions/<id>/review \
  -H "X-API-Key: change-me" -H "Content-Type: application/json" \
  -d '{"node":"failure_handling","reviewer":"alice","decision":"confirm","note":"verified"}'
```

Available templates: `GET /templates`. Reports are decision-support only, always
require human review, and do not evaluate accent, attractiveness, emotion,
personality, protected traits, or unrelated communication style.

## Resume/JD parsing and rubrics

The interview engine is no longer limited to the seeded demo claim. The
deterministic document module extracts skills from resume/JD text and builds a
competency rubric grounded in actual resume sentences:

```python
from pyinterviewbot_ai.documents import build_rubric

rubric = build_rubric(job_description_text, resume_text, max_competencies=4)
for competency in rubric.competencies:
    print(competency.name, competency.evidence_prompts)
```

The ai-service exposes `POST /documents/parse` with `{"text": ..., "kind":
"resume", "job_description": ...}`. PDF parsing is optional via `pypdf`.
No network/LLM call is involved, so claims cannot be hallucinated.

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
voice-runtime/      Rust — provider contracts, runtime lifecycle, events, mocks
ai-service/         Python — ASR/LLM/TTS orchestration, evidence engine, SQLite
  providers/        local/mock speech/language contracts
  scripts/          e2e_ws_test.py — full real-pipeline integration test
candidate-client/   Vite/TS — candidate voice UI + /recruiter view
docs/
  ARCHITECTURE.md   Layered design, what's real vs. simplified, bugs found
```

## Documentation

- [docs/ROADMAP.md](docs/ROADMAP.md) — completed and pending phases with verification status.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — layered design, what's real vs. simplified,
  and the two timing bugs found and fixed while building the integration test.
- [docs/internal/ROADMAP_HONEST.md](docs/internal/ROADMAP_HONEST.md) — exactly what's tested, built-but-unverified, and
  not built.
- [CHANGELOG.md](CHANGELOG.md) — release history.
- [SECURITY.md](SECURITY.md) — reporting and current security gaps.
- [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md) — threat model and required controls.
- [docs/CAPABILITY_MATRIX.md](docs/CAPABILITY_MATRIX.md) — native Rust runtime vs local
  models, licences and supported capabilities.
- [docs/adr/](docs/adr/) — architecture decision records.
- [docs/internal/BASELINE_AUDIT.md](docs/internal/BASELINE_AUDIT.md) — dated baseline audit
  and upstream research.

## Known issues and deferred work

**Open:**
- TLS/WSS, per-session authorization/tenant isolation, rate limits/quotas, audio
  retention/consent controls, bias/validation study, live CI E2E for real models.
- Live managed-call provider verification (workflow is contract-tested only).

**Rejected / deferred:**
- Production hiring without compliance review.
- Wholesale rewrite of working Python domain logic into Rust.

Full shipped/blocked/rejected detail: [`docs/internal/QUEUE_STATUS.md`](docs/internal/QUEUE_STATUS.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to set up and test changes, and
[SECURITY.md](SECURITY.md) for the current security gaps. CI runs `cargo fmt`/`clippy`/`test`
for the gateway, `ruff` + `pytest` for `ai-service`, and `tsc` + `npm run build` for the
candidate client. Python tests use mock providers and require no models or network.

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
