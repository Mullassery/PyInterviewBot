# Licence inventory (inspection snapshot)

Snapshot date: 2026-10-25. Not legal advice. Verify before distribution.

## This repository

- PyInterviewBot source: **Apache-2.0** (`LICENSE`).
- The repo does **not** include Langfuse or a Langfuse dependency.

## Runtime dependencies (verify resolved versions in lockfiles)

| Component | Declared/observed licence | Notes |
|---|---|---|
| Rust crates (`Cargo.lock`) | Mixed permissive licences | `cargo audit`/`cargo deny` recommended; no RustSec advisories reported in prior audit |
| FastAPI / Starlette / Uvicorn | MIT/BSD-style | Verify installed versions |
| faster-whisper | MIT (upstream) | Model weights have separate terms |
| Ollama client / Ollama runtime | MIT (upstream) | Model weights have separate terms |
| piper-tts | GPL-3.0 (Piper 1 GPL fork) | Distribution/embedding obligations apply; verify before shipping |
| httpx / aiosqlite / pydantic | BSD/MIT/Apache-style | Verify installed versions |
| Rust `voice-runtime` deps (`reqwest`, `hmac`, `sha2`, `hex`, `subtle`, `serde`, `tokio`, `uuid`) | Apache/MIT/BSD-style | Verify resolved crates in `Cargo.lock` |
| `sherpa-onnx` (optional `local-neural` feature) | Apache-2.0 | ONNX Runtime and model weights have separate terms |
| Local neural model files (VITS/Piper voice, Whisper) | Upstream model terms | Not bundled; operator must verify each model card |
| Rust `axum` (gateway/native routes) | MIT | Verify resolved version |
| TypeScript / Vite / browser client deps | MIT/BSD-style | `npm audit` clean in prior audit |

## Model weights and voices

- faster-whisper model weights: upstream model terms, not covered by the
  Python package licence.
- Ollama model (`qwen2.5:7b-instruct` by default): upstream model terms.
- Piper voice (`en_US-lessac-medium`): verify the voice/model card terms before
  redistribution.

## Optional cloud LLM

- OpenRouter is an optional third-party service, not bundled code. Its API usage,
  model availability, pricing, data processing, retention, and region are governed
  by the operator's OpenRouter account and the selected upstream model's terms.
- No OpenRouter SDK is vendored; the adapter uses the documented
  OpenAI-compatible `/chat/completions` HTTP shape.

## Integration boundary

The voice runtime is implemented natively in Rust in this repository. It does
not vendor or embed third-party speech-studio application source.

## Obligations to check before release

1. Piper/voice redistribution terms.
2. Every downloaded ASR/TTS model card.
3. Preserve the repo's Apache-2.0 `LICENSE` and any required notices.
4. Managed-call provider account terms and data-processing requirements, if
   that optional workflow is enabled.
