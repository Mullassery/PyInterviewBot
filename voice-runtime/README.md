# voice-runtime

Self-hosted, provider-neutral Rust voice runtime for PyInterviewBot.

This crate owns the reliability-critical voice layer without owning interview
questions, evidence, or evaluation:

- `VoiceProvider` contract (start/end session, audio in, speech out, cancel,
  poll normalized events, capabilities, health).
- `VoiceRuntime` with max-session capacity, per-session sequence numbers,
  bounded PCM16 audio, text limits, interruption/cancellation, idle sweeping,
  provider error normalization, and a bounded event bus.
- `VoiceState` pure transition function.
- `MockVoiceProvider` for deterministic CI/local use.
- `studio` module: native speech-studio core with PCM16/WAV codecs, linear
  resampling, RMS/silence-trim/peak-normalize/concatenate/chunk processing,
  bounded audio/text, explicit in-memory recording/export and deletion, chunked
  TTS output, engine catalogue/health discovery, session lifecycle,
  cancellation, idle sweeping, and replaceable `SpeechEngine` contracts plus a
  mock engine.
- `neural` module (feature `local-neural`): real local VITS/Piper TTS and
  Whisper ASR through sherpa-onnx, with operator-supplied model paths and CPU
  default. Models are not bundled.
- `managed_call` module: optional server-side managed-call workflow with call
  create/get/end, exponential-backoff retries for retryable failures,
  constant-time Bearer/HMAC webhook verification, bounded idempotent event
  ingestion, and normalized provider events.

## Example

```rust
use std::sync::Arc;
use voice_runtime::{
    CompletionReason, MockVoiceProvider, RuntimeConfig, SessionConfig, VoiceRuntime,
    VoiceState,
};

# async fn demo() {
let runtime = VoiceRuntime::new(
    Arc::new(MockVoiceProvider::new()),
    RuntimeConfig::default(),
);
let mut events = runtime.subscribe();
let session = runtime.start_session(SessionConfig::default()).await.unwrap();
assert_eq!(runtime.session_state(session).await.unwrap(), VoiceState::Listening);
runtime.send_audio(session, vec![0u8; 3200]).await.unwrap();
let normalized = runtime.poll_events(session, 16).await.unwrap();
assert!(!normalized.is_empty());
runtime.end_session(session, CompletionReason::ClientEnded).await.unwrap();
# let _ = &mut events;
# }
```

Run tests:

```bash
cargo test -p voice-runtime
```

The crate contains no bundled model weights. The speech-studio mock path makes
no network calls; the optional managed-call workflow only calls the configured
provider base URL. Neural inference stays behind replaceable providers, as
required by ADR-0002.
