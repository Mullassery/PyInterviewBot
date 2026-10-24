# Competitive intelligence in roadmap and upgrade work

**Policy date:** 2026-10-19

Competitive findings are an input to product decisions, not a one-off report.
Every pending roadmap item, new feature, and routine dependency upgrade should
check the repository's [`AUDIT_FINDINGS.md`](AUDIT_FINDINGS.md) before work
starts, and record the decision in the PR or changelog.

## Required check before building

1. **Read `AUDIT_FINDINGS.md`.** Find the relevant competitor row and the
   ranked next actions. If the finding is stale, update it with a dated source.
2. **Name the competitor and the user problem.** A feature is not justified by
   "competitor has it". State the workflow it unlocks, who needs it, and how
   often. Mark unverified demand as a hypothesis.
3. **Prefer reuse over new code.** Check whether an existing Mullassery repo
   already provides the capability (see `BUNDLES.md` in the profile repo and
   the bundle role file in this repo, when present). Typical reuse:
   - crawling/fetching/robots/SSRF → **MudFish**
   - architecture maps / PR diffs → **CodeAtlas**
   - data-quality contracts / validation → **StatGuardian**
   - token counting / cost tables → **PyTokenCalc**
   - dependency graph / advisories / SBOM → **PyDeepMesh**
   - segmentation / CLV / churn → **ClusterAudienceKit**
   - activation / lineage / PII → **PyReverseETL**
   - fleet state sync → **SyncFleet** (only after conflict resolution)
4. **Choose one disposition:** build, integrate, defer, or reject. Write it
   down. A rejection with a reason is a successful outcome.
5. **For routine upgrades**, check whether the upgrade closes a known
   competitor gap or breaks a differentiated capability. Run the tests that
   cover the affected path; do not merge a green build that silently changes a
   user-visible contract.

## PR / changelog checklist

- [ ] Competitor and source URL/date recorded (or "no direct competitor").
- [ ] User impact and frequency stated; unverified demand labelled.
- [ ] Reuse check done against sibling repos/bundles.
- [ ] Failure mode and rollback considered (especially auth, PII, cost).
- [ ] Docs/roadmap status updated honestly.
- [ ] Tests or a manual verification command included.

## Review cadence

- Refresh the competitor row when a release changes the capability.
- Re-check the full `AUDIT_FINDINGS.md` at least quarterly and before a major
  release.
- If a finding is wrong or stale, correct it in the same PR that relies on it.

## Non-goals

- Copying features for parity.
- Adding a dependency just because a competitor uses it.
- Treating star counts or download counts as proof of user need.

## OSS gap analysis — 2026-10-24

**What matters most here:** provider abstraction and real transports. Hard-coded ASR/LLM/TTS and raw WebSocket PCM16 block service swaps and telephony; formalise the interfaces and adopt WebRTC/telephony via Pipecat or LiveKit.

Scope: PyInterviewBot 0.1.1 (Apache-2.0). Voice-first interview MVP: Rust WebSocket gateway (VAD, barge-in, deterministic session FSM), Python FastAPI ai-service (faster-whisper, Ollama `qwen2.5:7b-instruct`, Piper, SQLite), TypeScript candidate/recruiter client. Compared against open-source real-time voice-agent frameworks that a builder would use instead of this stack. Evidence: `README.md`, `AUDIT_FINDINGS.md`, `ROADMAP_HONEST.md`, `docs/ARCHITECTURE.md`, `CHANGELOG.md`, `ai-service/pyproject.toml`, `gateway/src`, and verified upstream pages. Where README and older roadmap disagree (auth), the CHANGELOG and source win.

### Comparable OSS projects
| Project | GitHub | License | Last activity | Why it is the comparison |
| --- | --- | --- | --- | --- |
| Pipecat | https://github.com/pipecat-ai/pipecat | BSD-2-Clause | release `v1.14.0`, 2026-10-24 | Python voice/multimodal agent framework with streaming STT/LLM/TTS, VAD, interruptions, WebRTC/WebSocket transports and 150+ integrations |
| LiveKit Agents | https://github.com/livekit/agents | Apache-2.0 | release `livekit-agents@1.8.9`, 2026-10-24 | Production WebRTC agent framework with semantic turn detection, telephony, and mix-and-match STT/LLM/TTS |
| Vocode | https://github.com/vocodedev/vocode-core | MIT | last release `v0.1.114a2`, 2024-08-07 | Older open-source voice-agent library for streaming conversations and phone calls; useful as the "what happens when the framework stalls" comparison |

### Capability comparison
| Capability | Ours | Pipecat | LiveKit Agents | Vocode | Gap |
| --- | --- | --- | --- | --- | --- |
| Real-time streaming voice loop | Yes | Yes | Yes | Yes | Ours is a working end-to-end pipeline, not a stub |
| Barge-in / interruption handling | Yes | Yes | Yes | Yes | Ours cancels Piper synthesis and signals the client; competitors generalise it across providers |
| VAD and end-of-turn detection | Yes | Yes | Yes | Partial | Ours uses earshot plus a fixed 700ms trailing-silence rule; LiveKit ships semantic turn detection |
| Provider abstraction (STT/LLM/TTS) | No | Yes | Yes | Yes | Ours hardcodes one implementation each; swapping means editing `asr.py`/`llm.py`/`tts.py` |
| WebRTC transport | No | Yes | Yes | Partial | Ours is raw WebSocket + PCM16; no NAT traversal, jitter buffer or mobile SDKs |
| Telephony (SIP/PSTN) | No | Yes | Yes | Yes | Absent in Ours; matters for phone screening |
| Local/offline model operation | Yes | Yes | Yes | Partial | Ours runs Whisper/Ollama/Piper with no API keys; competitor support varies by plugin |
| Deterministic interview state machine | Yes | Partial | Partial | No | Ours has a pure, 19-test FSM; Pipecat Flows and LiveKit workflows are generic conversation graphs |
| Evidence extraction and competency graph | Yes | No | No | No | Ours is the product layer; competitors are frameworks with no assessment model |
| Resume/JD-grounded rubric | Partial | No | No | No | `documents.py` builds a deterministic rubric from text; one seeded scenario remains hardcoded |
| Candidate transcript non-disclosure by protocol | Yes | No | No | No | Ours has no message type that can carry candidate transcript/score to the client |
| Auth, TLS and retention controls | Partial | Partial | Partial | Partial | Ours has API keys and loopback fail-closed but no TLS; retention is delete-only |

### Gaps that matter (ranked)
1. **No provider abstraction (absent).** `docs/ARCHITECTURE.md` says one concrete implementation each for ASR/LLM/TTS and the spec's `SpeechRecognizer`/`LanguageModel`/`SpeechSynthesizer` interfaces are not formalised. Pipecat and LiveKit let users swap 150+ / many services without touching the pipeline. Hurts anyone who needs cloud latency, non-English models, or a different local model. Effort: M.
2. **No WebRTC or telephony (absent).** Ours is raw WebSocket PCM16; there is no jitter buffer, mobile SDK, SIP/PSTN path or TURN story. Pipecat and LiveKit treat WebRTC and telephony as first-class transports. Hurts phone-screen and mobile use cases, which are the volume market for interviewing. Effort: L if built; S-M if delegated to LiveKit/Pipecat.
3. **No measured latency or quality evidence (absent).** `ROADMAP_HONEST.md` lists no scale/latency benchmarks, and the pipeline is sequential per turn with a fixed 700ms silence cut. Competitors market semantic turn detection and low latency. Hurts credibility with production buyers and makes regressions invisible. Effort: M to instrument, S to tune.
4. **Assessment controls are missing (absent).** No consent flow, no audio recording/playback, no bias/EEOC evaluation, no audit trail, no formatted report or export; `ROADMAP_HONEST.md` lists these as not built. This is a hiring-compliance category, so an MVP without them is not deployable regardless of voice quality. Hurts any real employer pilot. Effort: L.
5. **Generalisation beyond the demo is weak (weaker).** One hardcoded competency and scenario, no interview planner, no multi-language, no claim-verification status distinct from evidence confidence. The new deterministic document parser improves JD/resume grounding but does not yet feed a general rubric/planner loop. Hurts repeat use across roles. Effort: M.
6. **Operational surface is thin (weaker).** API keys exist, but no TLS, Dockerfile, rate limiting or CI E2E; ai-service tests cover auth and documents only, and the real pipeline test is local-only. LiveKit/Pipecat have broader test and deployment stories. Hurts self-hosting and contribution velocity. Effort: S-M.

### Where we are differentiated
- **Assessment, not just conversation:** evidence extraction, competency graph, adaptive probing and a recruiter view are built in; Pipecat, LiveKit and Vocode stop at the conversational pipeline.
- **Deterministic correctness:** the session FSM is a pure function with 19 unit tests, and transport correctness does not depend on LLM latency; barge-in and playback-duration handling were validated with real audio.
- **Candidate integrity by construction:** the WebSocket protocol has no transcript/score message type, so the candidate UI cannot leak evaluation data even if it tries.
- **Local-only, no-cloud loop:** faster-whisper + Ollama + Piper with no API keys, which matters for candidate PII and for demos without vendor accounts.
- **Grounded documents:** deterministic resume/JD skill extraction and rubric generation without an LLM call, so claims cannot be hallucinated.

### Disposition
- **Build:** interview planner and multi-competency rubric from parsed JD/resume; explicit claim-verification status; consent/retention configuration; recruiter report with export; latency instrumentation.
- **Integrate/reuse:** adopt Pipecat or LiveKit Agents for transports, turn detection and provider adapters instead of extending the custom media stack; reuse their client SDKs for mobile/WebRTC. Keep the Rust FSM only if it remains the authoritative interview state layer.
- **Defer/reject:** custom telephony/SIP, custom WebRTC stack, multi-language voice personas, and whiteboard/code-artifact modality until a compliant design partner exists; production hiring recommendations without bias/legal review (reject).

### Sources
- https://github.com/pipecat-ai/pipecat (accessed 2026-10-24) — BSD-2-Clause, release `v1.14.0` (2026-10-24), transports and service list
- https://docs.pipecat.ai/ (accessed 2026-10-24) — open-source voice/multimodal pipeline, 150+ services, WebRTC/WebSocket
- https://github.com/livekit/agents (accessed 2026-10-24) — Apache-2.0, release `livekit-agents@1.8.9` (2026-10-24), semantic turn detection and telephony
- https://github.com/vocodedev/vocode-core (accessed 2026-10-24) — MIT, last release `v0.1.114a2` (2024-08-07)
- Repo files inspected: `README.md`, `AUDIT_FINDINGS.md`, `ROADMAP_HONEST.md`, `COMPETITIVE_INTELLIGENCE.md`, `docs/ARCHITECTURE.md`, `CHANGELOG.md`, `ai-service/pyproject.toml`, `ai-service/src/pyinterviewbot_ai/{main,agent,documents,db}.py`, `gateway/src`
