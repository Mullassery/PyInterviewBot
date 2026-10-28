# Security Vulnerability Audit - PyInterviewBot

**Audit Date:** 2026-10-28
**Repository:** `/Users/georgimullassery/Desktop/Projects/PyInterviewBot`
**Scope:** ai-service source, live workflow routing, gateway request path, Python/Rust dependency locks

## Summary

- No high- or medium-severity source-code findings.
- One low-severity Bandit warning (`assert` in `tts.py`), false positive for a
  startup configuration check.
- Five dependency advisories were found initially (`certifi`, `filelock`) and
  fixed by refreshing the ai-service lockfile.
- Tenant isolation was added after the initial audit. The remaining identity
  risks are bearer principal keys without rotation/revocation, a single gateway
  key per process, and no per-user recruiter login.
- The earlier four SQL-injection findings are **not reproducible**.

## Commands and results

Run from `ai-service`:

| Command | Result |
|---|---|
| `uvx bandit -r src -f txt` | 1 Low: `B101 assert_used`, `src/pyinterviewbot_ai/tts.py:31`; 0 Medium/High |
| `uv run --with pip-audit pip-audit` (before refresh) | 5 advisories: `certifi 2026.7.22` (`PYSEC-2026-4320`), `filelock 4.0.0` (`CVE-2026-112940`, `CVE-2026-112913`, `CVE-2026-112907`) |
| `uv lock --upgrade-package certifi --upgrade-package filelock` | `certifi` -> `2026.10.23`; `filelock` -> `4.2.4` |
| `uv run --with pip-audit pip-audit` (after refresh) | **No known vulnerabilities found** |
| `uv run --group dev python -m pytest -q` | **145 passed**, 2 dependency deprecation warnings |
| `uv run --group dev ruff check .` | **27 pre-existing violations**, no new tenant-isolation violations |

Run from the repository root:

| Command | Result |
|---|---|
| `cargo audit` | 304 crate dependencies scanned; **no vulnerabilities reported** |
| `cargo test --workspace` | **102 passed** (57 gateway, 13 interview-domain, 32 voice-runtime) |
| `cargo clippy --workspace --all-targets -- -D warnings` | pass |

`cargo audit` loaded 1,381 RustSec advisories. The Homebrew `bandit` executable
is broken (`bad interpreter`), so the audit used the isolated `uvx` tool.

## Earlier SQL-injection findings

The prior report listed four High SQL-injection findings in `db.py`. None are
reproducible in the current source:

1. Dynamic `ALTER TABLE sessions ADD COLUMN {name} {definition}` interpolation
   was replaced by the fixed `SESSION_COLUMN_MIGRATIONS` tuple.
2. Compliance updates now use fixed `COALESCE(?, column)` statements with
   parameterized values; injected column names are ignored.
3. Interview-metadata updates use the same fixed `COALESCE` shape. This also
   repaired the former 13-placeholders-for-14-columns insert bug.
4. The duplicate metadata reader was removed; session pin/migration statements
   are constants, not caller-controlled identifiers.

All remaining user data is bound through SQLite parameters. The new routing
module performs no SQL construction.

## Manual review

Reviewed `workflow_routing.py`, `agent.py`, `db.py`, `main.py`, `channels.py`,
and the Rust `AiClient` request path.

- **Authorization:** session, channel, compliance, and workflow routes require a
  tenant principal from `PYINTERVIEWBOT_PRINCIPALS`. A tenant is the principal
  subject; cross-tenant session/workflow IDs return `404`; the legacy global key
  returns `403` on tenant routes. `admin` may cross tenants. Session tokens are
  checked after tenant authorization and cannot widen access.
- **Workflow ownership:** routing checks owner before status/deployment checks,
  and workflow CRUD/audit/deployment/rollback checks owner before role checks,
  so foreign IDs are indistinguishable from missing IDs.
- **Channel isolation:** channel mappings and event idempotency keys include the
  owner subject, so the same `external_id` or `event_id` in two tenants creates
  two independent sessions/events.
- **Compliance isolation:** policy rows are keyed by tenant and seeded lazily;
  reset only affects the caller's tenant.
- **Migration:** session, channel, and compliance ownership is backfilled
  deterministically. Orphaned workflow pins fall back to the legacy owner; this
  is a data-classification operation, not interactive reconciliation.
- **Audit:** `session.start` is written in the same transaction as the session
  row and stores no claim/transcript text. Denied foreign-workflow starts are
  audited without disclosing workflow existence in the response.
- **SQL injection:** no f-string or concatenated SQL identifiers remain in the
  reviewed paths. Values are parameterized.
- **Secret exposure:** routed-start logging includes only workflow ID, version,
  content hash, and deployment ID. It excludes template bodies, transcripts,
  credentials, and binding fingerprints. Routing errors are generic and do not
  echo binding configuration.
- **Fail-open behavior:** routing fails closed. Unknown workflows return `404`;
  disabled workflows, missing active deployments, missing/changed bindings,
  missing versions, and hash mismatches return `409`. A corrupt pinned snapshot
  raises an internal error rather than silently falling back to a built-in.
- **Pin integrity:** the route is resolved before session insertion. Agent
  turns, reports, exports, priorities, and evidence review use the shared
  snapshot resolver. Channel resume ignores a newly supplied workflow ID and
  returns the stored pin.
- **Denial of service:** resolution adds bounded primary-key lookups. Snapshot
  parsing is limited to sessions already created from validated artifacts. No
  new unbounded loop or external network call was introduced.
- **Gateway:** `workflow_id` is optional, trimmed by configuration filtering,
  and forwarded as JSON only. It is not interpolated into URLs or logs.

## Residual risks

1. **[Medium] Principal keys are static bearer credentials.** There is no key
   rotation/revocation API, no per-user recruiter login, and no read-only
   recruiter role. A leaked tenant key exposes that tenant; a leaked `admin` key
   exposes all tenants.
2. **[Low] Gateway is single-tenant per process.** The gateway forwards one
   ai-service key. Its principal subject must match the workflow owner; sharing
   one gateway process across tenants would misattribute sessions.
3. **[Low] Audit coverage and retention.** Session starts, review, deletion, and
   report export are audited; reads and turns are not. Neither audit stream has
   automated retention or an operator UI.
4. **Existing platform risks remain.** No TLS/WSS termination in this repo,
   per-process rate limiting, and no live production provider verification.
5. **Migration caveat.** Sessions and channel rows assigned to the fallback
   legacy owner require an operator correction if the pre-upgrade owner is
   known to belong elsewhere.

The earlier High findings should be treated as resolved or non-reproducible.
The remaining risks are documented architectural boundaries, not regressions in
the live-routing change.
