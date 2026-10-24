# Voice capability and licence matrix

Inspected 2026-10-25. This is an engineering reference, not legal advice.
Verify model-weight terms separately from application-code licences before
distribution or commercial use.

| Capability | Native Rust runtime (`voice-runtime/`) | Python local models (default) |
|---|---|---|
| Deployment | In-repo workspace crate; transport-independent library | In-repo Rust gateway + Python service; models on host |
| Application licence | Apache-2.0 (this repo) | Apache-2.0 (this repo) |
| Model-weight terms | No weights bundled; neural feature uses operator-supplied VITS/Piper + Whisper | faster-whisper/Whisper, Ollama model, Piper voice: verify each |
| ASR | Speech-engine contract + mock; optional local Whisper via sherpa-onnx | faster-whisper per-utterance batch |
| TTS | Speech-engine contract + mock; optional local VITS/Piper via sherpa-onnx | Piper, streamed PCM16 |
| Audio processing | PCM16/WAV codec, resampling, RMS/trim/normalize/concat/chunk, explicit in-memory recording/export | Python-side model I/O |
| VAD / turn detection | Runtime lifecycle + gateway `earshot` VAD + configurable silence | `earshot` + configurable trailing silence |
| Interview state ownership | PyInterviewBot (runtime never owns questions/evidence) | PyInterviewBot (SQLite + deterministic FSM) |
| Barge-in / cancellation | Yes: interrupt transition + engine cancellation | Yes, cancels TTS and signals client |
| Bounded audio / capacity | Yes: per-session bytes, max sessions, text limit, idle sweep | Yes: gateway per-turn cap + ai-service ASR body cap |
| Event normalization | Yes: sequenced typed events | HTTP/gateway control messages |
| Managed-call workflow | Yes: server-side create/get/end, webhook auth, idempotent events | Not used |
| Telephony / WebRTC | No (transport-independent core) | No (raw WebSocket PCM16) |
| Offline / no account | Yes | Yes (after models installed) |
| Status in this repo | Implemented; mock and local neural paths tested | Working, tested |
| Cloud LLM option | OpenRouter adapter implemented, contract-tested (not live-verified) | Local/mock remain default |
| Cost | Infrastructure only | Infrastructure only |

## Decision

- Default to the local path.
- Self-host the voice runtime in Rust. Neural inference stays behind replaceable
  provider contracts; do not reimplement models without a documented reason.
- The managed-call workflow is optional, server-side only, and never owns
  interview sequencing/evidence. Keep private credentials off clients.
- Keep interview sequencing, evidence, reports, and retention ownership inside
  PyInterviewBot regardless of provider.
