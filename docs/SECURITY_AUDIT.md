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
- Two residual design risks remain: shared API-key access to workflow selection
  and missing audit rows for session creation.
- The earlier four SQL-injection findings are **not reproducible**.

## Commands and results

Run from `ai-service`:

| Command | Result |
|---|---|
| `uvx bandit -r src -f txt` | 1 Low: `B101 assert_used`, `src/pyinterviewbot_ai/tts.py:31`; 0 Medium/High |
| `uv run --with pip-audit pip-audit` (before refresh) | 5 advisories: `certifi 2026.7.22` (`PYSEC-2026-4320`), `filelock 4.0.0` (`CVE-2026-112940`, `CVE-2026-112913`, `CVE-2026-112907`) |
| `uv lock --upgrade-package certifi --upgrade-package filelock` | `certifi` -> `2026.10.23`; `filelock` -> `4.2.4` |
| `uv run --with pip-audit pip-audit` (after refresh) | **No known vulnerabilities found** |
| `uv run --group dev python -m pytest -q` | **125 passed**, 2 dependency deprecation warnings |
| `uv run --group dev ruff check .` | **27 pre-existing violations**, no new routing violations |

Run from the repository root:

| Command | Result |
|---|---|
| `cargo audit` | 304 crate dependencies scanned; **no vulnerabilities reported** |
| `cargo test --workspace` | **102 passed** (57 gateway, 13 interview-domain, 32 voice-runtime) |
| `cargo clippy --workspace --all-targets -- -D warnings` | pass |

`cargo audit` loaded 1,380 RustSec advisories. The Homebrew `bandit` executable
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

- **Authorization:** direct and channel session starts still use the shared
  ai-service API key. `workflow_id` does not add a separate authorization scope.
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

1. **[Medium] Shared-key workflow selection.** Any holder of the global API key
   can select any active production workflow by UUID and read any session UUID.
   There is no tenant isolation or per-workflow session authorization.
2. **[Low] Session-creation audit gap.** Workflow provenance is persisted on the
   session row, but creating a routed session does not append an
   `ai_audit_events` record.
3. **Existing platform risks remain.** No TLS/WSS termination in this repo,
   per-process rate limiting, no automated audit retention, and no live
   production provider verification.

The earlier High findings should be treated as resolved or non-reproducible.
The remaining risks are documented architectural boundaries, not regressions in
the live-routing change.
