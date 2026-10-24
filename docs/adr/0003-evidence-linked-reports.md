# ADR-0003: Evidence-linked reports with human review

- **Status:** Accepted
- **Date:** 2026-10-25
- **Deciders:** PyInterviewBot maintainers

## Context

The existing recruiter surface exposed raw evidence rows. There was no
structured report, no explicit link from a score to the transcript, no
separation between observed evidence and model inference, and no correction
workflow. Hiring use also creates a high-risk obligation not to score accent,
emotion, personality, protected characteristics, or unrelated communication
traits.

## Decision

We will generate reports only from persisted transcript and evidence data,
expose them at `GET /sessions/{session_id}/report`, and require human review.

Each rubric node reports:

- evidence status and an evidence-strength label (not a candidate score),
- citations containing the candidate transcript turn and quote,
- model-written notes explicitly flagged as inference,
- uncertainty derived from coverage/status,
- human review decisions from `POST /sessions/{session_id}/review`.

Reports always include `decision_support_only: true`, `human_review_required:
true`, and limitations that state the non-evaluation of protected/unrelated
traits. No hire/no-hire recommendation is produced.

## Consequences

- Recruiters can inspect the actual candidate quote behind an assessment.
- Human corrections are append-only and survive alongside model output.
- The report can be generated without calling an LLM, so it cannot introduce
  new unsupported claims.
- Scoring is limited to explicit rubric coverage; it is not a general
  personality or communication assessment.
- A future calibration study must not weaken the protected-trait exclusion or
  the human-review requirement.
