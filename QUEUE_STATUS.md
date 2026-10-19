# Queue status — 2026-10-19

This file records what was shipped, what is blocked/failed, and what is rejected for this repository. It complements `AUDIT_FINDINGS.md`.

## Shipped
- No code changes; audit/bundle docs added.
- API-key/Bearer auth for ai-service and gateway, loopback-only default.
- Session deletion endpoint for retention (`DELETE /sessions/{id}`).
- Deterministic resume/JD parsing and rubric generation (`POST /documents/parse`).

## Blocked / failed
- TLS, multi-scenario config, consent policy, bias evaluation, CI E2E.

## Rejected / deferred
- Production hiring without compliance review.

## Policy

Any future feature or upgrade must check `AUDIT_FINDINGS.md` and this file before implementation. A blocked item is retried only when its external dependency is available; a rejected item needs new user evidence or a design change to reopen.
