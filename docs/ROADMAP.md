# Roadmap: PyInterviewBot

> Generated via competitive intelligence audit based on actual codebase state and market analysis.

## Current State Assessment

- **Type:** Review codebase for specifics
- **Maturity:** Active development
- **Strengths:** Custom implementation tailored to use case; versioned workflow
  artifacts with optional live session routing
- **Gaps:** Documentation, testing, tenant isolation, ecosystem integration
  opportunities
- **Completed 2026-10-28:** opt-in direct/channel routing to active production
  workflow deployments with pinned template snapshots

## Competitive Landscape Analysis

**Key competitors/alternatives:** Evaluate against similar tools in the space. Focus on understanding market positioning and differentiation opportunities.

**Positioning Strategy:** Leverage local-first approach, performance, or specialized capabilities where possible.

## Critical Gaps to Fill

1. **Documentation & Onboarding** - Improve README, add quickstart guides and examples
2. **Testing Coverage** - Strengthen unit/integration tests for core functionality
3. **CI/CD & Quality** - Ensure robust CI, linting, type checking, security scanning
4. **Distribution** - Improve packaging and installation experience
5. **Ecosystem Integration** - Add integrations with popular tools in the category

## Roadmap by Timeline

### Short-term (0-30 days) - Quick Wins & Critical Fixes

| Item | Priority | Effort | Impact | Description |
|---|---|---|---|---|
| Documentation polish | P0 | S | High | Update README, add QUICKSTART.md, improve examples |
| Test coverage gaps | P0 | M | High | Add tests for critical/uncovered paths |
| CI hardening | P0 | S | High | Ensure all checks pass, add security scanning |
| Error messages & validation | P1 | S | High | Improve UX with better errors |
| Dependency updates | P1 | S | Med-High | Update dependencies, fix vulnerabilities |

### Medium-term (30-90 days) - Core Features & Integrations

| Item | Priority | Effort | Impact | Description |
|---|---|---|---|---|
| Key integrations | P0 | M | High | Add top 2-3 integrations for competitive parity |
| Performance optimization | P1 | M | High | Profile and optimize hot paths |
| API improvements | P1 | M | High | Enhance APIs/interfaces for better usability |
| Observability | P1 | M | High | Add logging, metrics, tracing |
| Advanced features | P2 | L | High | Features that differentiate from competitors |

### Long-term (90+ days) - Strategic Growth

| Item | Priority | Effort | Impact | Description |
|---|---|---|---|---|
| Ecosystem expansion | P2 | L | High | Plugin system or extension mechanism |
| Enterprise features | P2 | L | Med-High | Auth, RBAC, multi-tenancy if applicable |
| Scale & reliability | P2 | L | High | Improved resilience and scalability |
| Community building | P3 | L | Med | Docs site, examples gallery, contribution guides |
| Strategic integrations | P3 | L | Med-High | Deeper integrations with major platforms |

## Prioritized Feature List with Effort/Impact

| Feature | Category | Effort (S/M/L/XL) | Impact (1-10) | Priority Score | Rationale |
|---|---|---|---|---|---|
| Core stability fixes | Tech Debt | S | 10 | 10.0 | Foundation for everything |
| Documentation overhaul | DX | S | 9 | 9.0 | Lowers adoption barrier |
| Key integration/add-on | Feature | M | 9 | 6.0 | Closes competitive gap |
| Performance improvements | Performance | M | 8 | 5.3 | Better user experience |
| Differentiating feature | Feature | L | 9 | 4.5 | Competitive advantage |

## Success Metrics

| Metric | Current (baseline) | 30-day Target | 90-day Target | Measurement |
|---|---|---|---|---|
| Test coverage | Unknown/varies | >70% | >85% | Coverage reports |
| Documentation completeness | Partial | Core guides complete | Full API/docs reference | Manual review |
| CI pass rate | Needs verification | 100% on main | 100% on main | CI dashboard |
| Issue triage | N/A | <48h response | <24h response | GitHub metrics |
| Adoption signals | N/A | Track usage | Growing trend | Package stats/downloads |

## Technical Debt to Address

| Debt Item | Severity | Effort | Impact if Unaddressed | Plan |
|---|---|---|---|---|
| Missing/incomplete tests | High | M | Bugs slip through | Prioritize critical paths |
| Outdated dependencies | Med-High | M | Security/performance issues | Regular updates, scanning |
| Code quality issues | Med | M | Maintainability suffers | Incremental refactoring |
| Documentation debt | High | S-M | Adoption friction | Write as features evolve |
| Type safety/typing | Med | M | DX issues | Gradual improvement |

## Implementation Notes

- **Focus on differentiation:** Identify unique strengths and amplify them
- **Stay lean:** Solve real problems better than alternatives
- **Local-first advantage:** Emphasize privacy/offline/local control where applicable
- **Quality over quantity:** Do fewer things exceptionally well
- **Evidence-based:** Base decisions on codebase analysis and user needs

## Next Steps

1. Review and validate against actual user needs
2. Create GitHub issues for P0 items
3. Set up milestones/tracking
4. Execute short-term items first
5. Review quarterly and adjust

## Strategic feature additions (2026-11-04)

Source: the 2026-11-04 competitive ecosystem research (verified-audit of five
identifiable-by-pattern open implementations plus a full self-audit of both
platform repos; internal dossier kept outside this repo — no competitor
products are named in-repo). Every feature below was reproduced in evidence
in competing code, verified against our source, and is scoped by the
ecosystem boundary rule: Voxora owns telephony/voice-session infra
deliberately not duplicated here; interview intelligence stays here.

### S1 — Turn-policy execution (P0, S)

`AgentTurnDecision.next_action` (probe / clarify / challenge / move_on /
simulate / conclude) is produced and normalized but **never consumed by
business code** — only `is_complete` and `question_text` are
(`ai-service/.../agent.py:295-331`). Build the controller semantics: move_on
and conclude short-circuit the bank with the next pick; clarify/challenge
demand follow-ups from the current evidence node before progressing;
simulate triggers the mock interlocutor only (never live LLM); unknown values
fail closed to probe. Tests: each action's turn behavior + unknown enum
closed + conclude honors `PYINTERVIEWBOT_MAX_TURNS`.

### S2 — Follow-up probe governor (P0, S)

Competing practice-verified pattern (P11): weak answers trigger exactly one
probing follow-up, **capped per question**, with a `followUpsOf` provenance
field persisted. We have the vocabulary, no governor: evidence updates land
but no per-question cap or provenance row exists. Add `question_probes`
(count map keyed by session+evidence node, in-memory first, persisted at turn
commit) and a `referenced_turn` field for report citations. Acceptance:
5 weak consecutive answers ⇒ ≤2 probes unless the policy escalates
 (`challenge` overrides the cap by explicit recruiter permission flag);
report links each probe to its parent question. Threshold action rules same
as S1.

### S3 — Question crib payload in template spec (P1, M)

P12 pattern: per-question cribs — follow-ups, ideal-answer sketch, common
mistakes, **silent** (unspoken) rubric score guidance — are first-class
template data fed straight into the turn prompt, adapting depth without
schema churn. Our template JSON already banks questions; extend the question
schema (`question, followups[], common_mistakes[], guidance`, optional
`probe_level`) and thread cribs through `_turn_prompt` (respecting
`compliance._safe_question` and never echoing them in answers; add a
prompt-leak test asserting the model cannot read cribs aloud — competing
implementation got this persona drift wrong in its UI). Migration: depth
backfill each existing bank with derivation guidance only where safe
(that is gradable without invention); uncribbed questions degrade to plain
follow-up rules — honest caps, no fabricated rubrics.

### S4 — Question dedup + diversity gate (P1, S)

P13 pattern: fingerprint store (exact hash + light n-gram overlap) preventing
repeated questions across turns/builtin banks. We rebuild per-turn from fixed
bank order so repeats surface once a candidate stalls. Implement
`providers/bank.py` fingerprint (session-level + cross-session bank lint
callable) with a `QuestionDiversity` module rejecting fired hashes and
specifying similarity ceiling. Tests: repeated emerge only under normal
rotation; forced repeat when bank exhausted (honest, capped, flagged).

### S5 — Grading method attribution + confidence bands (P1, M)

P14 pattern (review-quality auditing): every evidence update records *how*
it was judged — `graded_by: agent_decision | llm_rescore | heuristic` — and
reports state scoring as a **band** where input is contested, never a lone
digit. We deliberately report citations, no scores; extend `evidence` rows
with `graded_by` (persisted from the decision source: voxora decide, Ollama
turn, or local fallback) and surface distribution-quality in `report.py`
(`confidence: high|medium|low` from prior-node state, evidence density).
Acceptance: report shows method mix + nodes with low-confidence labels;
mock-regression test pins determinism.

### S6 — JD → draft template generation (P2, M→L)

P15 pattern both vetted references implement (one as plain-language
generation, one as attached-resume/JD pipeline we verified end-to-end as the
competitor's own fork feature): describe-or-attach a JD; draft a full
template draft with inferred competency tree + question drafts. Build as a
**workflow-lifecycle feature**, not a new engine: generation writes a
`workflow` draft in the existing sandbox → enforced human approval round
via existing POSTs — no automated promotion, no direct DB writes, full
`WorkflowArtifact` provenance (generation model, model_profile, request
context preserved). Tests: artisan-rejection path, sandbox isolation,
snapshot pinning, approval gate still mandatory.

### S7 — Question refinement loop (P2, S)

P16 pattern: master question refinement endpoint — "make this probe 20%
narrower", returning rewrites to edit, never direct-bank mutation without
versioning. One small prompt + diff-apply against versions with the same
policy as S6.

### S8 — Live-coding + whiteboard artifacts (P3, XXL, defer)

P17/P18 patterns (Monaco canvas, Excalidraw board + rubric-hidden tasks +
blended heuristic/vision evaluation with confidence-band verdict). Strong
differentiator for senior technical screening but heavy: defer until S1-S5
are green and the voice-live loop (Voxora M6/M7) is stable. Sketch only:
question type extension, artifact storage on EvidenceGraphState (URL +
type), evaluator service. No work started in this slice; mark EXPLORATORY.

### S9 — Communication-signal ingestion (P2, M)

P19 pattern: pauses / words-per-minute / filler-word estimates from
speech analytics feed the **communication competency** evidence nodes on our
side (owner boundary clean: Voxora computes and exposes them per MCP M8 in
her roadmap, we consume from the transcript metadata; if Voxora is on-sim
compute locally from audio). Acceptance: report lists communication-signal
based evidence-notes with the exact source tagged; absent signals ⇒ no
communication datapoints — never zero-fabricate (picked report limits
wording already).

### S10 — Optional later explorations (P3)

- Multi-language localized assessment (competing contract enforces
  English-first localized text with a validated language matrix
  (10 codes), mixed code-switching STT mode): adopt the contract shape in
  template spec + turn prompts only when the user demands language support;
  do NOT silently half-support a locale state.
- Anti-cheat/integrity signals (tab-visibility, paste anomalies,
  multi-display detection): meaningful only with identity + consent config
  and a "cheat event" schema; the client-enforcement-only variant is
  spoofable in the reference and needs thought before any investment.
- IRT / adaptive question selection: experimental in one reference,
  flag-disabled; keep on hmm list for evidence-tree depth scaling later.

## Non-goals (rejected patterns from the same research)

- Per-session question randomization via temperature+random prompt ranges as
  the sole adaptive mechanism — rejected: it breaks cross-candidate
  comparability (verified as the only mechanism in one reference practice
  tool; screening requires reproducible ranges).
- Prompt titles/personas that can drift from tests (verified: one reference
  shipped a UI/persona mismatch its own tests caught elsewhere): any persona
  change ships with its snapshot test the same PR.
- Prompt-injection-resistant practice tricks don't apply to screening: our
  `_safe_question` + compliance patterns remain the sanctioned prompt layer;
  do not copy practice-tool softness into audit-grade boundaries.
