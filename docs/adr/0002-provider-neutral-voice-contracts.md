# ADR-0002: Provider-neutral speech and language contracts

- **Status:** Accepted
- **Date:** 2026-10-25
- **Deciders:** PyInterviewBot maintainers

## Context

The Python service imported faster-whisper, Ollama, and Piper directly in the
interview path. Swapping a speech engine, running CI without models, or adding
a local voice service required editing interview code. The Rust gateway already
talked to ai-service over a narrow HTTP contract, so the missing boundary was
inside Python.

The upstream options differ materially:

- The required voice runtime must be self-hosted and owned in this repository.
- Managed call providers are proprietary platforms. Their SDKs are clients, not
  self-hosted runtimes.
- Local faster-whisper/Ollama/Piper remain the only path with no account and no
  network dependency.

## Decision

We will define versioned internal protocols for `SpeechToText`, `TextToSpeech`,
and `LanguageModel`, select implementations through environment variables, and
keep interview sequencing/evidence ownership in the agent.

- Providers live under `ai-service/src/pyinterviewbot_ai/providers/`.
- `PYINTERVIEWBOT_ASR_PROVIDER`, `PYINTERVIEWBOT_TTS_PROVIDER`, and
  `PYINTERVIEWBOT_LLM_PROVIDER` accept `local` or `mock`.
- A global `PYINTERVIEWBOT_PROVIDER` fallback exists for local demos.
- `mock` is deterministic, offline, and never presented as real transcription
  or synthesis.
- The self-hosted Rust `voice-runtime/` crate owns session lifecycle, events,
  bounded audio, cancellation, timeouts, and provider contracts.
- The managed-call workflow is an optional server-side implementation for call
  creation and webhook authentication/normalization, not a replacement runtime.
- Provider errors are normalized with retryability and status context; unknown
  providers fail closed instead of silently falling back.

## Consequences

- CI can run a complete interview flow without Ollama, Piper voices, Whisper
  weights, network access, or paid APIs.
- Operators can select local models or a managed provider without changing
  interview business logic.
- The adapter surface is additive; existing local modules still work.
- New provider adapters must include contract tests and must not own interview
  state or evaluation truth.
- Model weights have separate terms from application code and must be reviewed
  before deployment.
