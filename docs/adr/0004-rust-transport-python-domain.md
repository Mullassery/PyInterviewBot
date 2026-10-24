# ADR-0004: Keep Rust transport and Python domain ownership

- **Status:** Accepted
- **Date:** 2026-10-25
- **Deciders:** PyInterviewBot maintainers

## Context

The requested direction is Rust-first for the voice runtime. The existing
repository already has a Rust gateway owning WebSocket transport, VAD,
barge-in, and session turn-taking, while Python owns ASR/LLM/TTS, evidence,
persistence, and interview decisions. Rewriting all of Python in Rust solely
for language uniformity would discard working local-model integration and add
large risk without a demonstrated scaling need.

## Decision

We will keep the split:

- Rust owns real-time transport, VAD, turn-taking, cancellation, bounded audio,
  and gateway-side reliability.
- Python remains the domain source of truth for templates, evidence, reports,
  retention, and model-provider adapters.
- The HTTP boundary between them stays narrow and versioned through Pydantic
  request/response schemas.

We will introduce separate Rust services only when GPU isolation, independent
scaling, or operational constraints require them.

## Consequences

- Existing working behavior and local models are preserved.
- Rust hardening work is focused on reliability-critical paths.
- Cross-language contract tests remain necessary; the gateway now forwards
  auth/template fields and has bounded audio behavior tested in Rust.
- Future Rust speech providers can implement the same normalized event
  contract without forcing a Python rewrite.
