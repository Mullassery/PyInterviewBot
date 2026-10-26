# ADR-0005: Rust gateway owns canonical call state

- **Status:** Accepted
- **Date:** 2026-10-26
- **Deciders:** PyInterviewBot maintainers

## Context

Twilio, Vapi, WhatsApp, and future providers can each report status, delivery,
failure, and completion events. Before this decision, the gateway logged Twilio
status callbacks and kept managed-call events in an in-memory workflow. There was
no durable canonical answer to “what happened to this call?”, no idempotent
provider-event log, and no reliable call↔interview correlation.

The Python ai-service already owns interview sessions, transcripts, evidence,
reports, and deletion. Giving it a second call-lifecycle writer would create a
dual-writer consistency problem, especially when Twilio/Vapi callbacks arrive
out of order or are retried.

## Decision

1. The Rust gateway owns a dedicated call-state SQLite database and is the sole
   application-level writer.
2. The call database is separate from the ai-service interview database. There
   is no cross-database transaction or foreign key.
3. Provider adapters normalize into the `interview-domain` call state machine.
   Capability resolution is conservative and never silently downgrades.
4. Status/event handling is idempotent and ordered:
   - unique internal idempotency keys,
   - unique provider call IDs,
   - `(call_id, provider_event_id)` deduplication,
   - monotonic adapter sequences,
   - durable unmatched-event replay after provider linking,
   - optimistic row versions.
5. The ai-service remains the system of record for interviews and talks to the
   gateway through the authenticated `/calls` API.
6. SQLite is accepted for the current single-node gateway. It must not be shared
   over a network filesystem. Multi-replica deployment requires a shared store
   and explicit single-writer/leader design.
7. Migrations are versioned with `PRAGMA user_version`, additive, and backed up
   before execution. Newer schemas are refused by older binaries.

## Consequences

Positive:

- One durable answer to call state, independent of provider retries.
- Provider callbacks can arrive before call creation and still be reconciled.
- Python cannot accidentally fork call state.
- Failures, duplicates, and out-of-order updates are observable and testable.

Negative / constraints:

- Every call read/write is a gateway dependency; ai-service cannot query call
  state locally.
- The gateway and ai-service must reconcile two stores during deletion/retention.
- SQLite limits horizontal scaling and assumes a single active writer.
- Provider-specific mapping still needs live account validation.

## Alternatives considered

- **Python owns all call state.** Rejected: it creates a dual writer with
  provider webhooks terminating in Rust and blurs interview/call ownership.
- **One shared SQLite database.** Rejected: couples unrelated schemas, makes
  migrations and backups riskier, and creates cross-service lock contention.
- **Postgres immediately.** Deferred: operationally heavier than needed for the
  current single-node slice. Revisit when multiple gateway replicas are needed.
- **Log-only lifecycle.** Rejected: cannot enforce idempotency, correlation, or
  recovery after restart.

## References

- [`docs/CALL_STATE.md`](../CALL_STATE.md)
- [`gateway/src/calls.rs`](../../gateway/src/calls.rs)
- [`interview-domain/src/call.rs`](../../interview-domain/src/call.rs)
