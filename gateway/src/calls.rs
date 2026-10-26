//! Canonical call lifecycle owned by the Rust gateway.
//!
//! SQLite in this module is the only writable store for call lifecycle state.
//! The Python interview service owns interview evidence and talks to these
//! calls through the authenticated gateway API; it never opens this database.

use std::collections::VecDeque;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::{
    extract::{Path as AxumPath, Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use chrono::Utc;
use interview_domain::call::{
    self, CallDirection, CallError, CallProvider, CallRequirements, CallResolutionError,
    CallSession, CallState, CallTransition, CallTransport, ProviderRegistry,
};
use rusqlite::{params, Connection, OptionalExtension, Row};
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use thiserror::Error;
use tracing::{info, warn};
use uuid::Uuid;

use crate::audit::record_admin;
use crate::ws_handler::{authorized, AppState};

const SCHEMA_VERSION: i64 = 1;
const BUSY_TIMEOUT_MS: u64 = 5_000;
const MAX_IDEMPOTENCY_KEY_CHARS: usize = 200;

const CALL_COLUMNS: &str = "id, interview_session_id, provider, provider_call_id, direction,
    transport, state, version, last_sequence, created_at_ms, updated_at_ms, ended_at_ms,
    terminal_transition";

#[derive(Debug, Error)]
pub enum CallStoreError {
    #[error("call database error: {0}")]
    Database(#[from] rusqlite::Error),
    #[error("call serialization error: {0}")]
    Serialization(#[from] serde_json::Error),
    #[error("call {0} not found")]
    NotFound(Uuid),
    #[error("call transition rejected: {0}")]
    Call(#[from] CallError),
    #[error("provider call id is already linked to another call")]
    Conflict,
    #[error("call changed concurrently; retry with fresh state")]
    ConcurrentModification,
    #[error("provider event id is required")]
    EmptyEventId,
    #[error("a call id or provider call id is required")]
    MissingLookup,
    #[error("provider mismatch: expected {expected}, found {found}")]
    ProviderMismatch {
        expected: CallProvider,
        found: CallProvider,
    },
    #[error("call provider {0} is not configured")]
    UnsupportedProvider(CallProvider),
    #[error("provider {0} cannot satisfy the call requirements")]
    UnsupportedCapabilities(CallProvider),
    #[error("invalid call request: {0}")]
    InvalidRequest(String),
    #[error("database schema version {found} is newer than supported {supported}")]
    SchemaTooNew { found: i64, supported: i64 },
}

impl CallStoreError {
    fn status_code(&self) -> StatusCode {
        match self {
            Self::InvalidRequest(_) | Self::Call(CallError::IllegalTransition { .. }) => {
                StatusCode::BAD_REQUEST
            }
            Self::NotFound(_) => StatusCode::NOT_FOUND,
            Self::Conflict
            | Self::ConcurrentModification
            | Self::Call(
                CallError::TerminalState { .. }
                | CallError::StaleEvent { .. }
                | CallError::ProviderCallConflict
                | CallError::EmptyProviderCallId,
            ) => StatusCode::CONFLICT,
            Self::UnsupportedProvider(_) => StatusCode::SERVICE_UNAVAILABLE,
            Self::UnsupportedCapabilities(_)
            | Self::MissingLookup
            | Self::EmptyEventId
            | Self::ProviderMismatch { .. } => StatusCode::BAD_REQUEST,
            Self::Database(_) | Self::Serialization(_) | Self::SchemaTooNew { .. } => {
                StatusCode::INTERNAL_SERVER_ERROR
            }
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "outcome", rename_all = "snake_case")]
pub enum ApplyOutcome {
    Applied { call: CallSession },
    Ignored { call: CallSession, reason: String },
    Duplicate { call: CallSession },
    Unmatched,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "outcome", rename_all = "snake_case")]
pub enum BeginOutcome {
    Created { call: CallSession },
    Existing { call: CallSession },
}

#[derive(Debug, Clone, Serialize)]
pub struct CallTransitionRecord {
    pub from: CallState,
    pub to: CallState,
    pub transition: CallTransition,
    pub sequence: Option<u64>,
    pub occurred_at_ms: u64,
    pub recorded_at_ms: u64,
}

#[derive(Debug, Clone, Serialize)]
pub struct CallHealth {
    pub integrity_ok: bool,
    pub schema_version: i64,
    pub calls: i64,
    pub transitions: i64,
    pub unmatched_events: i64,
}

#[derive(Clone)]
pub struct CallWorkflow {
    conn: Arc<Mutex<Connection>>,
    registry: Arc<ProviderRegistry>,
    path: Option<PathBuf>,
}

impl CallWorkflow {
    pub fn open(
        path: impl AsRef<Path>,
        registry: ProviderRegistry,
    ) -> Result<Self, CallStoreError> {
        let path = path.as_ref().to_path_buf();
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|error| CallStoreError::InvalidRequest(error.to_string()))?;
        }
        let conn = Connection::open(&path)?;
        let workflow = Self {
            conn: Arc::new(Mutex::new(conn)),
            registry: Arc::new(registry),
            path: Some(path),
        };
        workflow.init()?;
        Ok(workflow)
    }

    #[cfg(test)]
    pub fn in_memory(registry: ProviderRegistry) -> Result<Self, CallStoreError> {
        let conn = Connection::open_in_memory()?;
        let workflow = Self {
            conn: Arc::new(Mutex::new(conn)),
            registry: Arc::new(registry),
            path: None,
        };
        workflow.init()?;
        Ok(workflow)
    }

    pub fn from_env(registry: ProviderRegistry) -> Option<Arc<Self>> {
        let enabled = std::env::var("GATEWAY_CALLS_ENABLED")
            .ok()
            .is_some_and(|value| {
                matches!(
                    value.to_ascii_lowercase().as_str(),
                    "1" | "true" | "yes" | "on"
                )
            });
        if !enabled {
            return None;
        }
        let path =
            std::env::var("GATEWAY_CALL_DB_PATH").unwrap_or_else(|_| "data/calls.db".to_string());
        match Self::open(path, registry) {
            Ok(workflow) => Some(Arc::new(workflow)),
            Err(error) => {
                warn!(%error, "call workflow disabled");
                None
            }
        }
    }

    fn init(&self) -> Result<(), CallStoreError> {
        let conn = self.conn.lock().map_err(|_| {
            CallStoreError::InvalidRequest("call database lock poisoned".to_string())
        })?;
        conn.busy_timeout(Duration::from_millis(BUSY_TIMEOUT_MS))?;
        conn.execute_batch(
            "PRAGMA journal_mode=WAL;
             PRAGMA foreign_keys=ON;
             PRAGMA synchronous=NORMAL;",
        )?;
        migrate(&conn)?;
        sync_providers(&conn, &self.registry)?;
        Ok(())
    }

    pub fn registry(&self) -> &ProviderRegistry {
        &self.registry
    }

    pub fn path(&self) -> Option<&Path> {
        self.path.as_deref()
    }

    #[cfg(test)]
    pub fn begin_call(
        &self,
        provider: CallProvider,
        interview_session_id: Option<Uuid>,
        direction: CallDirection,
        transport: CallTransport,
        idempotency_key: Option<&str>,
        at_ms: u64,
    ) -> Result<CallSession, CallStoreError> {
        match self.begin_call_outcome(
            provider,
            interview_session_id,
            direction,
            transport,
            idempotency_key,
            at_ms,
        )? {
            BeginOutcome::Created { call } | BeginOutcome::Existing { call } => Ok(call),
        }
    }

    #[allow(clippy::too_many_arguments)]
    pub fn begin_call_outcome(
        &self,
        provider: CallProvider,
        interview_session_id: Option<Uuid>,
        direction: CallDirection,
        transport: CallTransport,
        idempotency_key: Option<&str>,
        at_ms: u64,
    ) -> Result<BeginOutcome, CallStoreError> {
        self.ensure_provider(provider, direction, transport)?;
        let idempotency_key = idempotency_key
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(|value| {
                value
                    .chars()
                    .take(MAX_IDEMPOTENCY_KEY_CHARS)
                    .collect::<String>()
            });
        let mut conn = self.lock()?;
        let transaction = conn.transaction()?;
        if let Some(key) = idempotency_key.as_deref() {
            if let Some(existing) = load_by_idempotency(&transaction, key)? {
                if existing.provider != provider
                    || existing.interview_session_id != interview_session_id
                {
                    return Err(CallStoreError::Conflict);
                }
                transaction.commit()?;
                return Ok(BeginOutcome::Existing { call: existing });
            }
        }
        let mut call = CallSession::new(
            Uuid::new_v4(),
            interview_session_id,
            provider,
            direction,
            transport,
            at_ms,
        );
        call.apply(CallTransition::Validated, Some(1), at_ms)?;
        call.apply(CallTransition::Initiate, Some(2), at_ms)?;
        insert_call(&transaction, &call, idempotency_key.as_deref())?;
        record_transition(
            &transaction,
            call.id,
            CallState::Requested,
            CallState::Validating,
            CallTransition::Validated,
            Some(1),
            at_ms,
        )?;
        record_transition(
            &transaction,
            call.id,
            CallState::Validating,
            CallState::Initiating,
            CallTransition::Initiate,
            Some(2),
            at_ms,
        )?;
        transaction.commit()?;
        info!(
            call_id = %call.id,
            interview_session_id = ?call.interview_session_id,
            provider = %call.provider,
            "call initiated"
        );
        Ok(BeginOutcome::Created { call })
    }

    pub fn register_inbound(
        &self,
        provider: CallProvider,
        transport: CallTransport,
        provider_call_id: &str,
        interview_session_id: Option<Uuid>,
        at_ms: u64,
    ) -> Result<CallSession, CallStoreError> {
        let provider_call_id = provider_call_id.trim();
        if provider_call_id.is_empty() {
            return Err(CallStoreError::Call(CallError::EmptyProviderCallId));
        }
        self.ensure_provider(provider, CallDirection::Inbound, transport)?;
        let mut conn = self.lock()?;
        let transaction = conn.transaction()?;
        if let Some(existing) = load_by_provider_call(&transaction, provider_call_id)? {
            transaction.commit()?;
            return Ok(existing);
        }
        let mut call = CallSession::new(
            Uuid::new_v4(),
            interview_session_id,
            provider,
            CallDirection::Inbound,
            transport,
            at_ms,
        );
        call.apply(CallTransition::Validated, Some(1), at_ms)?;
        call.apply(CallTransition::Initiate, Some(2), at_ms)?;
        call.apply(CallTransition::Connect, Some(3), at_ms)?;
        call.apply(CallTransition::Activate, Some(4), at_ms)?;
        call.link_provider_call(provider_call_id)?;
        insert_call(&transaction, &call, None)?;
        for (from, to, event, sequence) in [
            (
                CallState::Requested,
                CallState::Validating,
                CallTransition::Validated,
                1,
            ),
            (
                CallState::Validating,
                CallState::Initiating,
                CallTransition::Initiate,
                2,
            ),
            (
                CallState::Initiating,
                CallState::Connected,
                CallTransition::Connect,
                3,
            ),
            (
                CallState::Connected,
                CallState::Active,
                CallTransition::Activate,
                4,
            ),
        ] {
            record_transition(
                &transaction,
                call.id,
                from,
                to,
                event,
                Some(sequence),
                at_ms,
            )?;
        }
        transaction.commit()?;
        info!(call_id = %call.id, provider = %call.provider, "inbound call registered");
        Ok(call)
    }

    pub fn link_provider_call(
        &self,
        call_id: Uuid,
        provider_call_id: &str,
    ) -> Result<CallSession, CallStoreError> {
        let mut conn = self.lock()?;
        let transaction = conn.transaction()?;
        let mut call =
            load_by_id(&transaction, call_id)?.ok_or(CallStoreError::NotFound(call_id))?;
        if let Some(other) = load_by_provider_call(&transaction, provider_call_id)? {
            if other.id != call_id {
                return Err(CallStoreError::Conflict);
            }
        }
        call.link_provider_call(provider_call_id)?;
        update_call(&transaction, &call, call.version)?;
        replay_unmatched(&transaction, &mut call)?;
        update_call(&transaction, &call, call.version)?;
        let call = load_by_id(&transaction, call_id)?.ok_or(CallStoreError::NotFound(call_id))?;
        transaction.commit()?;
        Ok(call)
    }

    /// Applies a normalized provider target state. The provider event is
    /// recorded in the same transaction as the resulting transitions.
    #[allow(clippy::too_many_arguments)]
    pub fn apply_state(
        &self,
        provider: CallProvider,
        call_id: Option<Uuid>,
        provider_call_id: Option<&str>,
        provider_event_id: &str,
        target: CallState,
        sequence: Option<u64>,
        at_ms: u64,
    ) -> Result<ApplyOutcome, CallStoreError> {
        let provider_event_id = provider_event_id.trim();
        if provider_event_id.is_empty() {
            return Err(CallStoreError::EmptyEventId);
        }
        let mut conn = self.lock()?;
        let transaction = conn.transaction()?;
        let lookup = load_call(&transaction, call_id, provider_call_id)?;
        let Some(mut call) = lookup else {
            if let Some(provider_call_id) = provider_call_id.filter(|value| !value.is_empty()) {
                transaction.execute(
                    "INSERT OR IGNORE INTO unmatched_call_events
                     (provider, provider_call_id, provider_event_id, target_state, sequence, received_at_ms)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                    params![
                        provider.to_string(),
                        provider_call_id,
                        provider_event_id,
                        encode(&target)?,
                        sequence.map(|value| value as i64),
                        at_ms as i64,
                    ],
                )?;
                transaction.commit()?;
                return Ok(ApplyOutcome::Unmatched);
            }
            return Err(CallStoreError::NotFound(call_id.unwrap_or_default()));
        };
        if call.provider != provider {
            return Err(CallStoreError::ProviderMismatch {
                expected: provider,
                found: call.provider,
            });
        }
        if let Some(provider_call_id) = provider_call_id.filter(|value| !value.is_empty()) {
            if call.provider_call_id.is_none() {
                call.link_provider_call(provider_call_id)?;
                update_call(&transaction, &call, call.version)?;
            } else if call.provider_call_id.as_deref() != Some(provider_call_id) {
                return Err(CallStoreError::Conflict);
            }
        }
        if event_exists(&transaction, call.id, provider_event_id)? {
            transaction.commit()?;
            return Ok(ApplyOutcome::Duplicate { call });
        }
        let expected_version = call.version;
        let plan = path_to(call.state, target);
        let outcome = match plan {
            None => ApplyOutcome::Ignored {
                call: call.clone(),
                reason: format!("no valid transition from {:?} to {:?}", call.state, target),
            },
            Some(transitions) if transitions.is_empty() => ApplyOutcome::Ignored {
                call: call.clone(),
                reason: format!("call is already {:?}", target),
            },
            Some(transitions) => {
                for (index, transition) in transitions.iter().enumerate() {
                    let from = call.state;
                    let event_sequence = sequence.map(|base| base.saturating_add(index as u64));
                    match call.apply(*transition, event_sequence, at_ms) {
                        Ok(to) => record_transition(
                            &transaction,
                            call.id,
                            from,
                            to,
                            *transition,
                            event_sequence,
                            at_ms,
                        )?,
                        Err(error) => {
                            let outcome = ApplyOutcome::Ignored {
                                call: call.clone(),
                                reason: error.to_string(),
                            };
                            insert_event(
                                &transaction,
                                &call,
                                provider_event_id,
                                target,
                                "ignored",
                                at_ms,
                            )?;
                            transaction.commit()?;
                            return Ok(outcome);
                        }
                    }
                }
                update_call(&transaction, &call, expected_version)?;
                ApplyOutcome::Applied { call: call.clone() }
            }
        };
        let label = match &outcome {
            ApplyOutcome::Applied { .. } => "applied",
            ApplyOutcome::Ignored { .. } => "ignored",
            ApplyOutcome::Duplicate { .. } => "duplicate",
            ApplyOutcome::Unmatched => "unmatched",
        };
        insert_event(&transaction, &call, provider_event_id, target, label, at_ms)?;
        transaction.commit()?;
        Ok(outcome)
    }

    pub fn get(&self, call_id: Uuid) -> Result<Option<CallSession>, CallStoreError> {
        let conn = self.lock()?;
        load_by_id(&conn, call_id)
    }

    pub fn get_by_provider_call(
        &self,
        provider_call_id: &str,
    ) -> Result<Option<CallSession>, CallStoreError> {
        let conn = self.lock()?;
        load_by_provider_call(&conn, provider_call_id)
    }

    pub fn get_by_idempotency(
        &self,
        idempotency_key: &str,
    ) -> Result<Option<CallSession>, CallStoreError> {
        let conn = self.lock()?;
        load_by_idempotency(&conn, idempotency_key)
    }

    pub fn latest_for_interview(
        &self,
        interview_session_id: Uuid,
    ) -> Result<Option<CallSession>, CallStoreError> {
        let conn = self.lock()?;
        let row = conn
            .query_row(
                &format!(
                    "SELECT {CALL_COLUMNS} FROM calls
                     WHERE interview_session_id = ?1
                     ORDER BY created_at_ms DESC LIMIT 1"
                ),
                params![interview_session_id.to_string()],
                read_call_row,
            )
            .optional()?;
        row.map(CallRow::into_call).transpose()
    }

    pub fn list_for_interview(
        &self,
        interview_session_id: Uuid,
    ) -> Result<Vec<CallSession>, CallStoreError> {
        let conn = self.lock()?;
        let mut statement = conn.prepare(&format!(
            "SELECT {CALL_COLUMNS} FROM calls
             WHERE interview_session_id = ?1
             ORDER BY created_at_ms ASC"
        ))?;
        let rows = statement.query_map(params![interview_session_id.to_string()], read_call_row)?;
        let mut calls = Vec::new();
        for row in rows {
            calls.push(row?.into_call()?);
        }
        Ok(calls)
    }

    pub fn transitions(&self, call_id: Uuid) -> Result<Vec<CallTransitionRecord>, CallStoreError> {
        let conn = self.lock()?;
        let mut statement = conn.prepare(
            "SELECT from_state, to_state, transition, sequence, occurred_at_ms, recorded_at_ms
             FROM call_transitions WHERE call_id = ?1 ORDER BY id ASC",
        )?;
        let rows = statement.query_map(params![call_id.to_string()], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, Option<i64>>(3)?,
                row.get::<_, i64>(4)?,
                row.get::<_, i64>(5)?,
            ))
        })?;
        let mut transitions = Vec::new();
        for row in rows {
            let (from, to, transition, sequence, occurred_at_ms, recorded_at_ms) = row?;
            transitions.push(CallTransitionRecord {
                from: decode(&from)?,
                to: decode(&to)?,
                transition: decode(&transition)?,
                sequence: sequence.map(|value| value as u64),
                occurred_at_ms: occurred_at_ms as u64,
                recorded_at_ms: recorded_at_ms as u64,
            });
        }
        Ok(transitions)
    }

    pub fn health(&self) -> Result<CallHealth, CallStoreError> {
        let conn = self.lock()?;
        let integrity: String = conn.query_row("PRAGMA quick_check", [], |row| row.get(0))?;
        let schema_version: i64 =
            conn.pragma_query_value(None, "user_version", |row| row.get(0))?;
        let calls: i64 = conn.query_row("SELECT COUNT(*) FROM calls", [], |row| row.get(0))?;
        let transitions: i64 =
            conn.query_row("SELECT COUNT(*) FROM call_transitions", [], |row| {
                row.get(0)
            })?;
        let unmatched_events: i64 =
            conn.query_row("SELECT COUNT(*) FROM unmatched_call_events", [], |row| {
                row.get(0)
            })?;
        Ok(CallHealth {
            integrity_ok: integrity.eq_ignore_ascii_case("ok"),
            schema_version,
            calls,
            transitions,
            unmatched_events,
        })
    }

    pub fn purge_terminal_before(&self, cutoff_ms: u64) -> Result<usize, CallStoreError> {
        let conn = self.lock()?;
        let deleted = conn.execute(
            "DELETE FROM calls WHERE ended_at_ms IS NOT NULL AND ended_at_ms < ?1",
            params![cutoff_ms as i64],
        )?;
        Ok(deleted)
    }

    pub fn purge_unmatched_before(&self, cutoff_ms: u64) -> Result<usize, CallStoreError> {
        let conn = self.lock()?;
        let deleted = conn.execute(
            "DELETE FROM unmatched_call_events WHERE received_at_ms < ?1",
            params![cutoff_ms as i64],
        )?;
        Ok(deleted)
    }

    pub fn ensure_requirements(
        &self,
        provider: CallProvider,
        requirements: &CallRequirements,
    ) -> Result<(), CallStoreError> {
        let registration = self
            .registry
            .providers()
            .iter()
            .find(|registration| registration.provider == provider)
            .ok_or(CallStoreError::UnsupportedProvider(provider))?;
        if !registration.configured {
            return Err(CallStoreError::UnsupportedProvider(provider));
        }
        let capabilities = provider.capabilities();
        if requirements
            .required_capabilities()
            .iter()
            .any(|capability| !capabilities.supports(*capability))
        {
            return Err(CallStoreError::UnsupportedCapabilities(provider));
        }
        Ok(())
    }

    fn ensure_provider(
        &self,
        provider: CallProvider,
        direction: CallDirection,
        transport: CallTransport,
    ) -> Result<(), CallStoreError> {
        self.ensure_requirements(
            provider,
            &CallRequirements {
                direction,
                transport,
                ..CallRequirements::default()
            },
        )
    }

    fn lock(&self) -> Result<std::sync::MutexGuard<'_, Connection>, CallStoreError> {
        self.conn
            .lock()
            .map_err(|_| CallStoreError::InvalidRequest("call database lock poisoned".to_string()))
    }
}

fn migrate(conn: &Connection) -> Result<(), CallStoreError> {
    let version: i64 = conn.pragma_query_value(None, "user_version", |row| row.get(0))?;
    if version > SCHEMA_VERSION {
        return Err(CallStoreError::SchemaTooNew {
            found: version,
            supported: SCHEMA_VERSION,
        });
    }
    if version < 1 {
        conn.execute_batch(
            "BEGIN IMMEDIATE;
             CREATE TABLE IF NOT EXISTS calls (
               id TEXT PRIMARY KEY,
               interview_session_id TEXT,
               provider TEXT NOT NULL,
               provider_call_id TEXT,
               direction TEXT NOT NULL,
               transport TEXT NOT NULL,
               state TEXT NOT NULL,
               version INTEGER NOT NULL,
               last_sequence INTEGER,
               created_at_ms INTEGER NOT NULL,
               updated_at_ms INTEGER NOT NULL,
               ended_at_ms INTEGER,
               terminal_transition TEXT,
               idempotency_key TEXT,
               correlation_id TEXT NOT NULL
             );
             CREATE UNIQUE INDEX IF NOT EXISTS idx_calls_provider_call_id
               ON calls(provider_call_id) WHERE provider_call_id IS NOT NULL;
             CREATE UNIQUE INDEX IF NOT EXISTS idx_calls_idempotency
               ON calls(idempotency_key) WHERE idempotency_key IS NOT NULL;
             CREATE INDEX IF NOT EXISTS idx_calls_interview
               ON calls(interview_session_id, created_at_ms);
             CREATE INDEX IF NOT EXISTS idx_calls_state
               ON calls(state, updated_at_ms);

             CREATE TABLE IF NOT EXISTS call_transitions (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               call_id TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
               from_state TEXT NOT NULL,
               to_state TEXT NOT NULL,
               transition TEXT NOT NULL,
               sequence INTEGER,
               occurred_at_ms INTEGER NOT NULL,
               recorded_at_ms INTEGER NOT NULL
             );
             CREATE INDEX IF NOT EXISTS idx_call_transitions_call
               ON call_transitions(call_id, id);

             CREATE TABLE IF NOT EXISTS call_events (
               call_id TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
               provider_event_id TEXT NOT NULL,
               target_state TEXT NOT NULL,
               outcome TEXT NOT NULL,
               received_at_ms INTEGER NOT NULL,
               PRIMARY KEY (call_id, provider_event_id)
             );

             CREATE TABLE IF NOT EXISTS unmatched_call_events (
               provider TEXT NOT NULL,
               provider_call_id TEXT NOT NULL,
               provider_event_id TEXT NOT NULL,
               target_state TEXT NOT NULL,
               sequence INTEGER,
               received_at_ms INTEGER NOT NULL,
               PRIMARY KEY (provider, provider_call_id, provider_event_id)
             );
             CREATE INDEX IF NOT EXISTS idx_unmatched_received
               ON unmatched_call_events(received_at_ms);

             CREATE TABLE IF NOT EXISTS call_providers (
               provider TEXT PRIMARY KEY,
               configured INTEGER NOT NULL,
               capabilities_json TEXT NOT NULL,
               updated_at_ms INTEGER NOT NULL
             );
             PRAGMA user_version = 1;
             COMMIT;",
        )?;
    }
    Ok(())
}

fn sync_providers(conn: &Connection, registry: &ProviderRegistry) -> Result<(), CallStoreError> {
    let now = Utc::now().timestamp_millis();
    for registration in registry.providers() {
        conn.execute(
            "INSERT INTO call_providers (provider, configured, capabilities_json, updated_at_ms)
             VALUES (?1, ?2, ?3, ?4)
             ON CONFLICT(provider) DO UPDATE SET
               configured=excluded.configured,
               capabilities_json=excluded.capabilities_json,
               updated_at_ms=excluded.updated_at_ms",
            params![
                registration.provider.to_string(),
                if registration.configured { 1 } else { 0 },
                encode(&registration.provider.capabilities())?,
                now,
            ],
        )?;
    }
    Ok(())
}

fn insert_call(
    conn: &Connection,
    call: &CallSession,
    idempotency_key: Option<&str>,
) -> Result<(), CallStoreError> {
    conn.execute(
        "INSERT INTO calls
         (id, interview_session_id, provider, provider_call_id, direction, transport, state,
          version, last_sequence, created_at_ms, updated_at_ms, ended_at_ms,
          terminal_transition, idempotency_key, correlation_id)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15)",
        params![
            call.id.to_string(),
            call.interview_session_id.map(|value| value.to_string()),
            call.provider.to_string(),
            call.provider_call_id,
            encode(&call.direction)?,
            encode(&call.transport)?,
            encode(&call.state)?,
            call.version as i64,
            call.last_sequence.map(|value| value as i64),
            call.created_at_ms as i64,
            call.updated_at_ms as i64,
            call.ended_at_ms.map(|value| value as i64),
            call.terminal_transition
                .map(|value| encode(&value))
                .transpose()?,
            idempotency_key,
            call.interview_session_id
                .map(|value| value.to_string())
                .unwrap_or_else(|| call.id.to_string()),
        ],
    )?;
    Ok(())
}

fn update_call(
    conn: &Connection,
    call: &CallSession,
    expected_version: u64,
) -> Result<(), CallStoreError> {
    let changed = conn.execute(
        "UPDATE calls SET
           interview_session_id=?1, provider=?2, provider_call_id=?3, direction=?4,
           transport=?5, state=?6, version=?7, last_sequence=?8, updated_at_ms=?9,
           ended_at_ms=?10, terminal_transition=?11
         WHERE id=?12 AND version=?13",
        params![
            call.interview_session_id.map(|value| value.to_string()),
            call.provider.to_string(),
            call.provider_call_id,
            encode(&call.direction)?,
            encode(&call.transport)?,
            encode(&call.state)?,
            call.version as i64,
            call.last_sequence.map(|value| value as i64),
            call.updated_at_ms as i64,
            call.ended_at_ms.map(|value| value as i64),
            call.terminal_transition
                .map(|value| encode(&value))
                .transpose()?,
            call.id.to_string(),
            expected_version as i64,
        ],
    )?;
    if changed != 1 {
        return Err(CallStoreError::ConcurrentModification);
    }
    Ok(())
}

fn record_transition(
    conn: &Connection,
    call_id: Uuid,
    from: CallState,
    to: CallState,
    transition: CallTransition,
    sequence: Option<u64>,
    at_ms: u64,
) -> Result<(), CallStoreError> {
    conn.execute(
        "INSERT INTO call_transitions
         (call_id, from_state, to_state, transition, sequence, occurred_at_ms, recorded_at_ms)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        params![
            call_id.to_string(),
            encode(&from)?,
            encode(&to)?,
            encode(&transition)?,
            sequence.map(|value| value as i64),
            at_ms as i64,
            Utc::now().timestamp_millis(),
        ],
    )?;
    Ok(())
}

fn insert_event(
    conn: &Connection,
    call: &CallSession,
    provider_event_id: &str,
    target: CallState,
    outcome: &str,
    at_ms: u64,
) -> Result<(), CallStoreError> {
    conn.execute(
        "INSERT INTO call_events
         (call_id, provider_event_id, target_state, outcome, received_at_ms)
         VALUES (?1, ?2, ?3, ?4, ?5)",
        params![
            call.id.to_string(),
            provider_event_id,
            encode(&target)?,
            outcome,
            at_ms as i64,
        ],
    )?;
    Ok(())
}

fn event_exists(
    conn: &Connection,
    call_id: Uuid,
    provider_event_id: &str,
) -> Result<bool, CallStoreError> {
    Ok(conn
        .query_row(
            "SELECT 1 FROM call_events WHERE call_id=?1 AND provider_event_id=?2",
            params![call_id.to_string(), provider_event_id],
            |_| Ok(()),
        )
        .optional()?
        .is_some())
}

fn replay_unmatched(conn: &Connection, call: &mut CallSession) -> Result<(), CallStoreError> {
    let Some(provider_call_id) = call.provider_call_id.clone() else {
        return Ok(());
    };
    let mut statement = conn.prepare(
        "SELECT provider_event_id, target_state, sequence, received_at_ms
         FROM unmatched_call_events
         WHERE provider=?1 AND provider_call_id=?2
         ORDER BY received_at_ms ASC, rowid ASC",
    )?;
    let rows = statement.query_map(
        params![call.provider.to_string(), provider_call_id],
        |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, Option<i64>>(2)?,
                row.get::<_, i64>(3)?,
            ))
        },
    )?;
    let mut pending = Vec::new();
    for row in rows {
        pending.push(row?);
    }
    for (event_id, target, sequence, received_at_ms) in pending {
        let target: CallState = decode(&target)?;
        let sequence = sequence.map(|value| value as u64);
        if event_exists(conn, call.id, &event_id)? {
            conn.execute(
                "DELETE FROM unmatched_call_events
                 WHERE provider=?1 AND provider_call_id=?2 AND provider_event_id=?3",
                params![call.provider.to_string(), provider_call_id, event_id],
            )?;
            continue;
        }
        let expected_version = call.version;
        let mut applied_any = false;
        let outcome = match path_to(call.state, target) {
            Some(transitions) if !transitions.is_empty() => {
                for (index, transition) in transitions.iter().enumerate() {
                    let from = call.state;
                    let event_sequence = sequence.map(|base| base.saturating_add(index as u64));
                    if let Ok(to) = call.apply(*transition, event_sequence, received_at_ms as u64) {
                        applied_any = true;
                        record_transition(
                            conn,
                            call.id,
                            from,
                            to,
                            *transition,
                            event_sequence,
                            received_at_ms as u64,
                        )?;
                    } else {
                        break;
                    }
                }
                "applied"
            }
            _ => "ignored",
        };
        if applied_any {
            update_call(conn, call, expected_version)?;
        }
        insert_event(
            conn,
            call,
            &event_id,
            target,
            outcome,
            received_at_ms as u64,
        )?;
        conn.execute(
            "DELETE FROM unmatched_call_events
             WHERE provider=?1 AND provider_call_id=?2 AND provider_event_id=?3",
            params![call.provider.to_string(), provider_call_id, event_id],
        )?;
    }
    Ok(())
}

#[derive(Debug)]
struct CallRow {
    id: String,
    interview_session_id: Option<String>,
    provider: String,
    provider_call_id: Option<String>,
    direction: String,
    transport: String,
    state: String,
    version: i64,
    last_sequence: Option<i64>,
    created_at_ms: i64,
    updated_at_ms: i64,
    ended_at_ms: Option<i64>,
    terminal_transition: Option<String>,
}

impl CallRow {
    fn into_call(self) -> Result<CallSession, CallStoreError> {
        Ok(CallSession {
            id: Uuid::parse_str(&self.id)
                .map_err(|error| CallStoreError::InvalidRequest(error.to_string()))?,
            interview_session_id: self
                .interview_session_id
                .map(|value| {
                    Uuid::parse_str(&value)
                        .map_err(|error| CallStoreError::InvalidRequest(error.to_string()))
                })
                .transpose()?,
            provider: CallProvider::parse(&self.provider)
                .ok_or(CallStoreError::InvalidRequest(self.provider))?,
            provider_call_id: self.provider_call_id,
            direction: decode(&self.direction)?,
            transport: decode(&self.transport)?,
            state: decode(&self.state)?,
            version: self.version as u64,
            last_sequence: self.last_sequence.map(|value| value as u64),
            created_at_ms: self.created_at_ms as u64,
            updated_at_ms: self.updated_at_ms as u64,
            ended_at_ms: self.ended_at_ms.map(|value| value as u64),
            terminal_transition: self
                .terminal_transition
                .map(|value| decode(&value))
                .transpose()?,
        })
    }
}

fn read_call_row(row: &Row<'_>) -> rusqlite::Result<CallRow> {
    Ok(CallRow {
        id: row.get(0)?,
        interview_session_id: row.get(1)?,
        provider: row.get(2)?,
        provider_call_id: row.get(3)?,
        direction: row.get(4)?,
        transport: row.get(5)?,
        state: row.get(6)?,
        version: row.get(7)?,
        last_sequence: row.get(8)?,
        created_at_ms: row.get(9)?,
        updated_at_ms: row.get(10)?,
        ended_at_ms: row.get(11)?,
        terminal_transition: row.get(12)?,
    })
}

fn load_by_id(conn: &Connection, call_id: Uuid) -> Result<Option<CallSession>, CallStoreError> {
    let row = conn
        .query_row(
            &format!("SELECT {CALL_COLUMNS} FROM calls WHERE id = ?1"),
            params![call_id.to_string()],
            read_call_row,
        )
        .optional()?;
    row.map(CallRow::into_call).transpose()
}

fn load_by_provider_call(
    conn: &Connection,
    provider_call_id: &str,
) -> Result<Option<CallSession>, CallStoreError> {
    let row = conn
        .query_row(
            &format!("SELECT {CALL_COLUMNS} FROM calls WHERE provider_call_id = ?1"),
            params![provider_call_id],
            read_call_row,
        )
        .optional()?;
    row.map(CallRow::into_call).transpose()
}

fn load_by_idempotency(
    conn: &Connection,
    idempotency_key: &str,
) -> Result<Option<CallSession>, CallStoreError> {
    let row = conn
        .query_row(
            &format!("SELECT {CALL_COLUMNS} FROM calls WHERE idempotency_key = ?1"),
            params![idempotency_key],
            read_call_row,
        )
        .optional()?;
    row.map(CallRow::into_call).transpose()
}

fn load_call(
    conn: &Connection,
    call_id: Option<Uuid>,
    provider_call_id: Option<&str>,
) -> Result<Option<CallSession>, CallStoreError> {
    if let Some(call_id) = call_id {
        if let Some(call) = load_by_id(conn, call_id)? {
            return Ok(Some(call));
        }
    }
    if let Some(provider_call_id) = provider_call_id.filter(|value| !value.is_empty()) {
        return load_by_provider_call(conn, provider_call_id);
    }
    if call_id.is_some() {
        return Ok(None);
    }
    Err(CallStoreError::MissingLookup)
}

fn encode<T: Serialize>(value: &T) -> Result<String, CallStoreError> {
    Ok(serde_json::to_string(value)?)
}

fn decode<T: DeserializeOwned>(value: &str) -> Result<T, CallStoreError> {
    Ok(serde_json::from_str(value)?)
}

const TRANSITIONS: [CallTransition; 14] = [
    CallTransition::Validated,
    CallTransition::Initiate,
    CallTransition::Ringing,
    CallTransition::Connect,
    CallTransition::Activate,
    CallTransition::BeginCompletion,
    CallTransition::Complete,
    CallTransition::Cancel,
    CallTransition::Decline,
    CallTransition::Busy,
    CallTransition::NoAnswer,
    CallTransition::Fail,
    CallTransition::Disconnect,
    CallTransition::Timeout,
];

pub fn path_to(from: CallState, to: CallState) -> Option<Vec<CallTransition>> {
    if from == to {
        return Some(Vec::new());
    }
    if from.is_terminal() {
        return None;
    }
    let mut queue = VecDeque::from([(from, Vec::<CallTransition>::new())]);
    let mut seen = vec![from];
    while let Some((state, path)) = queue.pop_front() {
        for transition in TRANSITIONS {
            let Ok(next) = call::transition(state, transition) else {
                continue;
            };
            if seen.contains(&next) {
                continue;
            }
            seen.push(next);
            let mut next_path = path.clone();
            next_path.push(transition);
            if next == to {
                return Some(next_path);
            }
            if !next.is_terminal() {
                queue.push_back((next, next_path));
            }
        }
    }
    None
}

pub fn twilio_target(status: &str) -> Option<CallState> {
    match status.trim().to_ascii_lowercase().as_str() {
        "queued" | "initiated" => Some(CallState::Initiating),
        "ringing" => Some(CallState::Ringing),
        "in-progress" => Some(CallState::Active),
        "completed" => Some(CallState::Completed),
        "busy" => Some(CallState::Busy),
        "no-answer" => Some(CallState::NoAnswer),
        "failed" => Some(CallState::Failed),
        "canceled" | "cancelled" => Some(CallState::Cancelled),
        _ => None,
    }
}

pub fn twilio_sequence(status: &str) -> Option<u64> {
    match status.trim().to_ascii_lowercase().as_str() {
        "queued" => Some(10),
        "initiated" => Some(20),
        "ringing" => Some(30),
        "in-progress" => Some(40),
        "completed" | "busy" | "no-answer" | "failed" | "canceled" | "cancelled" => Some(50),
        _ => None,
    }
}

pub fn vapi_target(status: &str, ended_reason: Option<&str>) -> Option<CallState> {
    match status.trim().to_ascii_lowercase().as_str() {
        "scheduled" | "queued" => Some(CallState::Initiating),
        "ringing" => Some(CallState::Ringing),
        "in-progress" | "forwarding" => Some(CallState::Active),
        "ended" => Some(vapi_ended_target(ended_reason)),
        _ => None,
    }
}

pub fn vapi_ended_target(ended_reason: Option<&str>) -> CallState {
    let Some(reason) = ended_reason else {
        return CallState::Completed;
    };
    let reason = reason.trim().to_ascii_lowercase();
    if reason.contains("no-answer")
        || reason.contains("not-answer")
        || reason.contains("unanswered")
        || reason.contains("did-not-answer")
    {
        CallState::NoAnswer
    } else if reason.contains("busy") {
        CallState::Busy
    } else if reason.contains("declin") || reason.contains("reject") {
        CallState::Declined
    } else if reason.contains("cancel") {
        CallState::Cancelled
    } else if reason.contains("timeout")
        || reason.contains("max-duration")
        || reason.contains("silence")
    {
        CallState::TimedOut
    } else if reason.contains("error")
        || reason.contains("failed")
        || reason.contains("not-found")
        || reason.contains("not_valid")
    {
        CallState::Failed
    } else {
        CallState::Completed
    }
}

pub fn vapi_sequence(status: &str) -> Option<u64> {
    match status.trim().to_ascii_lowercase().as_str() {
        "scheduled" => Some(5),
        "queued" => Some(10),
        "ringing" => Some(30),
        "in-progress" | "forwarding" => Some(40),
        "ended" => Some(50),
        _ => None,
    }
}

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/calls", get(list_calls).post(start_call))
        .route("/calls/health", get(calls_health))
        .route("/calls/providers", get(list_providers))
        .route("/calls/idempotency/{key}", get(get_by_idempotency))
        .route(
            "/calls/by-provider/{provider_call_id}",
            get(get_by_provider_call),
        )
        .route("/calls/{id}", get(get_call))
        .route("/calls/{id}/transitions", get(get_transitions))
        .route("/calls/{id}/end", post(end_call))
}

fn check_auth(
    state: &AppState,
    headers: &HeaderMap,
    query: &std::collections::HashMap<String, String>,
) -> Option<Response> {
    if authorized(headers, query, &state.api_key) {
        None
    } else {
        Some(StatusCode::UNAUTHORIZED.into_response())
    }
}

fn workflow(state: &AppState) -> Option<Arc<CallWorkflow>> {
    state.calls.clone()
}

fn call_disabled() -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({"error": "call workflow is disabled"})),
    )
        .into_response()
}

fn store_error(error: CallStoreError) -> Response {
    (
        error.status_code(),
        Json(json!({"error": error.to_string()})),
    )
        .into_response()
}

fn resolution_error(error: CallResolutionError) -> Response {
    match error {
        CallResolutionError::NoProvider => (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"error": "no call provider is configured"})),
        )
            .into_response(),
        CallResolutionError::Unsupported { provider, missing } => (
            StatusCode::BAD_REQUEST,
            Json(json!({
                "error": format!("provider {provider} cannot satisfy this call"),
                "missing": missing,
            })),
        )
            .into_response(),
    }
}

fn provider_error(provider: CallProvider) -> Response {
    (
        StatusCode::BAD_REQUEST,
        Json(
            json!({"error": format!("provider {provider} does not support live call initiation")}),
        ),
    )
        .into_response()
}

pub(crate) fn now_ms() -> u64 {
    Utc::now().timestamp_millis().max(0) as u64
}

async fn calls_health(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<std::collections::HashMap<String, String>>,
) -> Response {
    if let Some(response) = check_auth(&state, &headers, &query) {
        return response;
    }
    let Some(workflow) = workflow(&state) else {
        return call_disabled();
    };
    match workflow.health() {
        Ok(health) => Json(json!({
            "healthy": health.integrity_ok,
            "db_path": workflow.path().map(|path| path.display().to_string()),
            "database": health,
        }))
        .into_response(),
        Err(error) => store_error(error),
    }
}

async fn list_providers(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<std::collections::HashMap<String, String>>,
) -> Response {
    if let Some(response) = check_auth(&state, &headers, &query) {
        return response;
    }
    let Some(workflow) = workflow(&state) else {
        return call_disabled();
    };
    let providers: Vec<Value> = workflow
        .registry()
        .providers()
        .iter()
        .map(|registration| {
            json!({
                "provider": registration.provider,
                "configured": registration.configured,
                "capabilities": registration.provider.capabilities(),
            })
        })
        .collect();
    Json(json!({"providers": providers})).into_response()
}

#[derive(Debug, Deserialize)]
struct StartCallRequest {
    provider: Option<CallProvider>,
    interview_session_id: Uuid,
    direction: Option<CallDirection>,
    transport: Option<CallTransport>,
    to: Option<String>,
    template_id: Option<String>,
    payload: Option<Value>,
    requirements: Option<CallRequirements>,
    idempotency_key: Option<String>,
}

async fn start_call(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<std::collections::HashMap<String, String>>,
    Json(request): Json<StartCallRequest>,
) -> Response {
    if let Some(response) = check_auth(&state, &headers, &query) {
        return response;
    }
    let Some(workflow) = workflow(&state) else {
        return call_disabled();
    };
    let direction = request.direction.unwrap_or(CallDirection::Outbound);
    let transport = request.transport.unwrap_or(CallTransport::Pstn);
    if direction != CallDirection::Outbound {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": "POST /calls only starts outbound calls"})),
        )
            .into_response();
    }
    let mut requirements = request.requirements.unwrap_or_default();
    requirements.direction = direction;
    requirements.transport = transport;
    let provider = match request.provider {
        Some(provider) => {
            if let Err(error) = workflow.ensure_requirements(provider, &requirements) {
                return store_error(error);
            }
            provider
        }
        None => match workflow.registry().resolve(&requirements) {
            Ok(provider) => provider,
            Err(error) => return resolution_error(error),
        },
    };
    if !matches!(
        (provider, transport),
        (
            CallProvider::Twilio | CallProvider::Vapi,
            CallTransport::Pstn
        )
    ) {
        return provider_error(provider);
    }
    let _ = request.template_id;
    let outcome = match workflow.begin_call_outcome(
        provider,
        Some(request.interview_session_id),
        direction,
        transport,
        request.idempotency_key.as_deref(),
        now_ms(),
    ) {
        Ok(outcome) => outcome,
        Err(error) => return store_error(error),
    };
    let (call, created) = match outcome {
        BeginOutcome::Created { call } => (call, true),
        BeginOutcome::Existing { call } => (call, false),
    };
    if !created {
        return (StatusCode::OK, Json(json!({"call": call}))).into_response();
    }

    let external = match provider {
        CallProvider::Twilio => {
            let Some(config) = state.twilio.clone() else {
                return fail_call(
                    &state,
                    &workflow,
                    provider,
                    call,
                    "Twilio is not configured",
                );
            };
            let Some(to) = request
                .to
                .as_deref()
                .map(str::trim)
                .filter(|value| !value.is_empty())
            else {
                return fail_call(
                    &state,
                    &workflow,
                    provider,
                    call,
                    "to is required for Twilio",
                );
            };
            config
                .create_call_with_ref(to, &request.interview_session_id.to_string(), Some(call.id))
                .await
        }
        CallProvider::Vapi => {
            let Some(managed) = state.managed_call.clone() else {
                return fail_call(&state, &workflow, provider, call, "Vapi is not configured");
            };
            let Some(mut payload) = request.payload else {
                return fail_call(
                    &state,
                    &workflow,
                    provider,
                    call,
                    "payload is required for Vapi",
                );
            };
            if let Err(error) =
                inject_vapi_metadata(&mut payload, call.id, request.interview_session_id)
            {
                return fail_call(&state, &workflow, provider, call, &error);
            }
            managed
                .client
                .create_call(payload)
                .await
                .map(|created| created.id)
                .map_err(|error| error.to_string())
        }
        _ => return provider_error(provider),
    };

    let provider_call_id = match external {
        Ok(provider_call_id) => provider_call_id,
        Err(error) => {
            return fail_call(&state, &workflow, provider, call, &error);
        }
    };
    let call = match workflow.link_provider_call(call.id, &provider_call_id) {
        Ok(call) => call,
        Err(error) => {
            warn!(%error, call_id = %call.id, "failed to link provider call id");
            return store_error(error);
        }
    };
    record_admin(
        &state,
        "call.create",
        &call.id.to_string(),
        "success",
        &provider.to_string(),
    );
    (
        StatusCode::CREATED,
        Json(json!({"call": call, "provider_call_id": provider_call_id})),
    )
        .into_response()
}

fn fail_call(
    state: &AppState,
    workflow: &CallWorkflow,
    provider: CallProvider,
    call: CallSession,
    reason: &str,
) -> Response {
    let sequence = call.last_sequence.unwrap_or(0).saturating_add(1);
    let failed = workflow
        .apply_state(
            provider,
            Some(call.id),
            None,
            &format!("local:{}:initiation-failed", call.id),
            CallState::Failed,
            Some(sequence),
            now_ms(),
        )
        .ok()
        .and_then(|outcome| match outcome {
            ApplyOutcome::Applied { call }
            | ApplyOutcome::Ignored { call, .. }
            | ApplyOutcome::Duplicate { call } => Some(call),
            ApplyOutcome::Unmatched => None,
        })
        .unwrap_or(call);
    record_admin(
        state,
        "call.create",
        &failed.id.to_string(),
        "failed",
        &reason.chars().take(200).collect::<String>(),
    );
    (
        StatusCode::BAD_GATEWAY,
        Json(json!({"error": reason, "call": failed})),
    )
        .into_response()
}

fn inject_vapi_metadata(
    payload: &mut Value,
    call_id: Uuid,
    interview_session_id: Uuid,
) -> Result<(), String> {
    let object = payload
        .as_object_mut()
        .ok_or_else(|| "payload must be a JSON object".to_string())?;
    let metadata = object
        .entry("metadata")
        .or_insert_with(|| json!({}))
        .as_object_mut()
        .ok_or_else(|| "payload.metadata must be a JSON object".to_string())?;
    metadata.insert("pyinterviewbot_call_id".to_string(), json!(call_id));
    metadata.insert(
        "interview_session_id".to_string(),
        json!(interview_session_id),
    );
    Ok(())
}

async fn list_calls(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<std::collections::HashMap<String, String>>,
) -> Response {
    if let Some(response) = check_auth(&state, &headers, &query) {
        return response;
    }
    let Some(workflow) = workflow(&state) else {
        return call_disabled();
    };
    if let Some(provider_call_id) = query
        .get("provider_call_id")
        .filter(|value| !value.is_empty())
    {
        return match workflow.get_by_provider_call(provider_call_id) {
            Ok(Some(call)) => Json(json!({"calls": [call]})).into_response(),
            Ok(None) => Json(json!({"calls": []})).into_response(),
            Err(error) => store_error(error),
        };
    }
    let Some(interview_id) = query
        .get("interview_session_id")
        .and_then(|value| Uuid::parse_str(value).ok())
    else {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": "interview_session_id or provider_call_id is required"})),
        )
            .into_response();
    };
    if query.get("latest").is_some_and(|value| value == "true") {
        return match workflow.latest_for_interview(interview_id) {
            Ok(Some(call)) => Json(json!({"call": call})).into_response(),
            Ok(None) => (
                StatusCode::NOT_FOUND,
                Json(json!({"error": "call not found"})),
            )
                .into_response(),
            Err(error) => store_error(error),
        };
    }
    match workflow.list_for_interview(interview_id) {
        Ok(calls) => Json(json!({"calls": calls})).into_response(),
        Err(error) => store_error(error),
    }
}

async fn get_call(
    State(state): State<AppState>,
    AxumPath(call_id): AxumPath<Uuid>,
    headers: HeaderMap,
    Query(query): Query<std::collections::HashMap<String, String>>,
) -> Response {
    if let Some(response) = check_auth(&state, &headers, &query) {
        return response;
    }
    let Some(workflow) = workflow(&state) else {
        return call_disabled();
    };
    match workflow.get(call_id) {
        Ok(Some(call)) => Json(json!({"call": call})).into_response(),
        Ok(None) => (
            StatusCode::NOT_FOUND,
            Json(json!({"error": "call not found"})),
        )
            .into_response(),
        Err(error) => store_error(error),
    }
}

async fn get_by_provider_call(
    State(state): State<AppState>,
    AxumPath(provider_call_id): AxumPath<String>,
    headers: HeaderMap,
    Query(query): Query<std::collections::HashMap<String, String>>,
) -> Response {
    if let Some(response) = check_auth(&state, &headers, &query) {
        return response;
    }
    let Some(workflow) = workflow(&state) else {
        return call_disabled();
    };
    match workflow.get_by_provider_call(&provider_call_id) {
        Ok(Some(call)) => Json(json!({"call": call})).into_response(),
        Ok(None) => (
            StatusCode::NOT_FOUND,
            Json(json!({"error": "call not found"})),
        )
            .into_response(),
        Err(error) => store_error(error),
    }
}

async fn get_by_idempotency(
    State(state): State<AppState>,
    AxumPath(key): AxumPath<String>,
    headers: HeaderMap,
    Query(query): Query<std::collections::HashMap<String, String>>,
) -> Response {
    if let Some(response) = check_auth(&state, &headers, &query) {
        return response;
    }
    let Some(workflow) = workflow(&state) else {
        return call_disabled();
    };
    match workflow.get_by_idempotency(&key) {
        Ok(Some(call)) => Json(json!({"call": call})).into_response(),
        Ok(None) => (
            StatusCode::NOT_FOUND,
            Json(json!({"error": "call not found"})),
        )
            .into_response(),
        Err(error) => store_error(error),
    }
}

async fn get_transitions(
    State(state): State<AppState>,
    AxumPath(call_id): AxumPath<Uuid>,
    headers: HeaderMap,
    Query(query): Query<std::collections::HashMap<String, String>>,
) -> Response {
    if let Some(response) = check_auth(&state, &headers, &query) {
        return response;
    }
    let Some(workflow) = workflow(&state) else {
        return call_disabled();
    };
    match workflow.transitions(call_id) {
        Ok(transitions) => Json(json!({"transitions": transitions})).into_response(),
        Err(error) => store_error(error),
    }
}

async fn end_call(
    State(state): State<AppState>,
    AxumPath(call_id): AxumPath<Uuid>,
    headers: HeaderMap,
    Query(query): Query<std::collections::HashMap<String, String>>,
) -> Response {
    if let Some(response) = check_auth(&state, &headers, &query) {
        return response;
    }
    let Some(workflow) = workflow(&state) else {
        return call_disabled();
    };
    let call = match workflow.get(call_id) {
        Ok(Some(call)) => call,
        Ok(None) => {
            return (
                StatusCode::NOT_FOUND,
                Json(json!({"error": "call not found"})),
            )
                .into_response()
        }
        Err(error) => return store_error(error),
    };
    if call.state.is_terminal() {
        return Json(json!({"call": call})).into_response();
    }
    let Some(provider_call_id) = call.provider_call_id.clone() else {
        return (
            StatusCode::CONFLICT,
            Json(json!({"error": "call has not been linked to a provider", "call": call})),
        )
            .into_response();
    };
    let result = match call.provider {
        CallProvider::Twilio => match state.twilio.clone() {
            Some(config) => config.end_call(&provider_call_id).await,
            None => Err("Twilio is not configured".to_string()),
        },
        CallProvider::Vapi => match state.managed_call.clone() {
            Some(managed) => managed
                .client
                .end_call(&provider_call_id)
                .await
                .map(|_| ())
                .map_err(|error| error.to_string()),
            None => Err("Vapi is not configured".to_string()),
        },
        provider => return provider_error(provider),
    };
    if let Err(error) = result {
        return (
            StatusCode::BAD_GATEWAY,
            Json(json!({"error": error, "call": call})),
        )
            .into_response();
    }
    let sequence = call.last_sequence.unwrap_or(0).saturating_add(1);
    match workflow.apply_state(
        call.provider,
        Some(call.id),
        Some(&provider_call_id),
        &format!("local:{}:end", call.id),
        CallState::Cancelled,
        Some(sequence),
        now_ms(),
    ) {
        Ok(outcome) => {
            record_admin(
                &state,
                "call.end",
                &call.id.to_string(),
                "success",
                &call.provider.to_string(),
            );
            Json(json!({"outcome": outcome})).into_response()
        }
        Err(error) => store_error(error),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use interview_domain::call::{
        CallDirection, CallProvider, CallState, CallTransition, CallTransport,
        ProviderRegistration, ProviderRegistry,
    };
    use std::path::PathBuf;
    use uuid::Uuid;

    fn registry() -> ProviderRegistry {
        ProviderRegistry::new(vec![
            ProviderRegistration::new(CallProvider::Twilio, true),
            ProviderRegistration::new(CallProvider::Vapi, true),
            ProviderRegistration::new(CallProvider::WhatsApp, true),
        ])
    }

    fn workflow() -> CallWorkflow {
        CallWorkflow::in_memory(registry()).unwrap()
    }

    fn temp_path() -> PathBuf {
        std::env::temp_dir().join(format!("calls-{}.db", Uuid::new_v4()))
    }

    #[test]
    fn initiation_is_idempotent_and_lifecycle_is_durable() {
        let workflow = workflow();
        let interview = Uuid::new_v4();
        let call = workflow
            .begin_call(
                CallProvider::Twilio,
                Some(interview),
                CallDirection::Outbound,
                CallTransport::Pstn,
                Some("idem-1"),
                1_000,
            )
            .unwrap();
        assert_eq!(call.state, CallState::Initiating);
        let duplicate = workflow
            .begin_call(
                CallProvider::Twilio,
                Some(interview),
                CallDirection::Outbound,
                CallTransport::Pstn,
                Some("idem-1"),
                1_001,
            )
            .unwrap();
        assert_eq!(duplicate.id, call.id);

        workflow.link_provider_call(call.id, "CA1").unwrap();
        let applied = workflow
            .apply_state(
                CallProvider::Twilio,
                Some(call.id),
                Some("CA1"),
                "twilio:CA1:ringing",
                CallState::Ringing,
                Some(30),
                1_100,
            )
            .unwrap();
        assert!(matches!(applied, ApplyOutcome::Applied { .. }));
        let duplicate = workflow
            .apply_state(
                CallProvider::Twilio,
                Some(call.id),
                Some("CA1"),
                "twilio:CA1:ringing",
                CallState::Ringing,
                Some(30),
                1_101,
            )
            .unwrap();
        assert!(matches!(duplicate, ApplyOutcome::Duplicate { .. }));

        let active = workflow
            .apply_state(
                CallProvider::Twilio,
                Some(call.id),
                Some("CA1"),
                "twilio:CA1:in-progress",
                CallState::Active,
                Some(40),
                1_200,
            )
            .unwrap();
        let ApplyOutcome::Applied { call: active } = active else {
            panic!("expected active call");
        };
        assert_eq!(active.state, CallState::Active);

        let completed = workflow
            .apply_state(
                CallProvider::Twilio,
                Some(call.id),
                Some("CA1"),
                "twilio:CA1:completed",
                CallState::Completed,
                Some(50),
                1_300,
            )
            .unwrap();
        let ApplyOutcome::Applied { call: completed } = completed else {
            panic!("expected completed call");
        };
        assert_eq!(completed.state, CallState::Completed);

        let late = workflow
            .apply_state(
                CallProvider::Twilio,
                Some(call.id),
                Some("CA1"),
                "twilio:CA1:late-ringing",
                CallState::Ringing,
                Some(60),
                1_400,
            )
            .unwrap();
        assert!(matches!(late, ApplyOutcome::Ignored { .. }));
        assert_eq!(
            workflow.get(call.id).unwrap().unwrap().state,
            CallState::Completed
        );
        assert!(workflow.transitions(call.id).unwrap().len() >= 7);
        assert_eq!(
            workflow
                .latest_for_interview(interview)
                .unwrap()
                .unwrap()
                .id,
            call.id
        );
    }

    #[test]
    fn unmatched_events_replay_after_provider_link() {
        let workflow = workflow();
        let call = workflow
            .begin_call(
                CallProvider::Vapi,
                Some(Uuid::new_v4()),
                CallDirection::Outbound,
                CallTransport::Pstn,
                None,
                2_000,
            )
            .unwrap();
        let unmatched = workflow
            .apply_state(
                CallProvider::Vapi,
                None,
                Some("vapi-call-1"),
                "vapi:event-1",
                CallState::Active,
                Some(40),
                2_100,
            )
            .unwrap();
        assert!(matches!(unmatched, ApplyOutcome::Unmatched));
        let linked = workflow.link_provider_call(call.id, "vapi-call-1").unwrap();
        assert_eq!(linked.state, CallState::Active);
        assert_eq!(
            workflow
                .get_by_provider_call("vapi-call-1")
                .unwrap()
                .unwrap()
                .id,
            call.id
        );
    }

    #[test]
    fn provider_ids_cannot_be_stolen_by_another_call() {
        let workflow = workflow();
        let interview = Some(Uuid::new_v4());
        let first = workflow
            .begin_call(
                CallProvider::Twilio,
                interview,
                CallDirection::Outbound,
                CallTransport::Pstn,
                None,
                3_000,
            )
            .unwrap();
        let second = workflow
            .begin_call(
                CallProvider::Twilio,
                interview,
                CallDirection::Outbound,
                CallTransport::Pstn,
                None,
                3_001,
            )
            .unwrap();
        workflow.link_provider_call(first.id, "CA-shared").unwrap();
        let error = workflow
            .link_provider_call(second.id, "CA-shared")
            .unwrap_err();
        assert!(matches!(error, CallStoreError::Conflict));
    }

    #[test]
    fn state_survives_reopening_the_database() {
        let path = temp_path();
        let interview = Uuid::new_v4();
        let call_id = {
            let workflow = CallWorkflow::open(&path, registry()).unwrap();
            let call = workflow
                .begin_call(
                    CallProvider::Twilio,
                    Some(interview),
                    CallDirection::Outbound,
                    CallTransport::Pstn,
                    None,
                    4_000,
                )
                .unwrap();
            workflow
                .apply_state(
                    CallProvider::Twilio,
                    Some(call.id),
                    None,
                    "twilio:evt:active",
                    CallState::Active,
                    Some(40),
                    4_100,
                )
                .unwrap();
            call.id
        };
        let reopened = CallWorkflow::open(&path, registry()).unwrap();
        let call = reopened.get(call_id).unwrap().unwrap();
        assert_eq!(call.state, CallState::Active);
        assert_eq!(call.interview_session_id, Some(interview));
        let health = reopened.health().unwrap();
        assert!(health.integrity_ok);
        assert!(health.calls >= 1);
        drop(reopened);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn shortest_transition_paths_never_skip_required_states() {
        assert_eq!(
            path_to(CallState::Initiating, CallState::Active).unwrap(),
            vec![CallTransition::Connect, CallTransition::Activate]
        );
        assert_eq!(
            path_to(CallState::Active, CallState::Completed).unwrap(),
            vec![CallTransition::BeginCompletion, CallTransition::Complete]
        );
        assert!(path_to(CallState::Completed, CallState::Active).is_none());
        assert!(path_to(CallState::Connected, CallState::NoAnswer).is_none());
    }

    #[test]
    fn vapi_metadata_preserves_caller_fields_and_adds_correlation() {
        let call_id = Uuid::new_v4();
        let interview = Uuid::new_v4();
        let mut payload = json!({"metadata": {"tenant": "acme"}, "assistantId": "a1"});
        inject_vapi_metadata(&mut payload, call_id, interview).unwrap();
        assert_eq!(payload["metadata"]["tenant"], "acme");
        assert_eq!(
            payload["metadata"]["pyinterviewbot_call_id"],
            json!(call_id)
        );
        assert_eq!(
            payload["metadata"]["interview_session_id"],
            json!(interview)
        );
        let mut invalid = json!({"metadata": "not-an-object"});
        assert!(inject_vapi_metadata(&mut invalid, call_id, interview).is_err());
    }

    #[test]
    fn provider_statuses_map_to_canonical_states() {
        assert_eq!(twilio_target("ringing"), Some(CallState::Ringing));
        assert_eq!(twilio_target("in-progress"), Some(CallState::Active));
        assert_eq!(twilio_target("no-answer"), Some(CallState::NoAnswer));
        assert_eq!(twilio_target("canceled"), Some(CallState::Cancelled));
        assert_eq!(twilio_target("mystery"), None);

        assert_eq!(vapi_target("in-progress", None), Some(CallState::Active));
        assert_eq!(
            vapi_target("ended", Some("customer-ended-call")),
            Some(CallState::Completed)
        );
        assert_eq!(
            vapi_target("ended", Some("silence-timeout")),
            Some(CallState::TimedOut)
        );
        assert_eq!(
            vapi_target("ended", Some("pipeline-error-openai-voice-failed")),
            Some(CallState::Failed)
        );
        assert_eq!(
            vapi_target("ended", Some("customer-did-not-answer")),
            Some(CallState::NoAnswer)
        );
    }

    #[test]
    fn terminal_retention_is_bounded_and_explicit() {
        let workflow = workflow();
        let call = workflow
            .begin_call(
                CallProvider::Twilio,
                Some(Uuid::new_v4()),
                CallDirection::Outbound,
                CallTransport::Pstn,
                None,
                5_000,
            )
            .unwrap();
        assert_eq!(workflow.purge_terminal_before(12_000).unwrap(), 0);
        workflow
            .apply_state(
                CallProvider::Twilio,
                Some(call.id),
                None,
                "twilio:evt:failed",
                CallState::Failed,
                Some(10),
                11_000,
            )
            .unwrap();
        assert_eq!(workflow.purge_terminal_before(12_000).unwrap(), 1);
        assert!(workflow.get(call.id).unwrap().is_none());
    }
    fn test_state(workflow: CallWorkflow) -> AppState {
        use voice_runtime::{
            EngineCatalog, MockSpeechEngine, SpeechEngine, SpeechStudio, StudioConfig,
        };
        let engine: Arc<dyn SpeechEngine> = Arc::new(MockSpeechEngine::default());
        let studio = Arc::new(SpeechStudio::new(engine.clone(), StudioConfig::default()));
        let engines = Arc::new(EngineCatalog::new("mock").register(engine));
        AppState {
            ai: crate::ai_client::AiClient::new("http://127.0.0.1:9"),
            api_key: Some("test-key".to_string()),
            session_config: crate::session::SessionConfig::default(),
            studio,
            engines,
            managed_call: None,
            calls: Some(Arc::new(workflow)),
            twilio: None,
            scheduler: None,
            calendar: None,
            audit: None,
        }
    }

    async fn call_request(
        state: AppState,
        method: &str,
        uri: &str,
        body: Option<Value>,
    ) -> (StatusCode, Value) {
        use axum::body::Body;
        use axum::http::Request;
        use tower::ServiceExt;
        let mut builder = Request::builder()
            .method(method)
            .uri(uri)
            .header("authorization", "Bearer test-key");
        let body = match body {
            Some(value) => {
                builder = builder.header("content-type", "application/json");
                Body::from(value.to_string())
            }
            None => Body::empty(),
        };
        let response = router()
            .with_state(state)
            .oneshot(builder.body(body).unwrap())
            .await
            .unwrap();
        let status = response.status();
        let bytes = axum::body::to_bytes(response.into_body(), 1024 * 1024)
            .await
            .unwrap();
        let value = if bytes.is_empty() {
            json!({})
        } else {
            serde_json::from_slice(&bytes).unwrap_or_else(|_| json!({}))
        };
        (status, value)
    }

    #[tokio::test]
    async fn calls_api_requires_auth_and_reports_providers() {
        let state = test_state(workflow());
        use axum::body::Body;
        use axum::http::Request;
        use tower::ServiceExt;
        let unauthorized = router()
            .with_state(state.clone())
            .oneshot(
                Request::builder()
                    .uri("/calls/providers")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(unauthorized.status(), StatusCode::UNAUTHORIZED);

        let (status, body) = call_request(state, "GET", "/calls/providers", None).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["providers"].as_array().unwrap().len(), 3);
    }

    #[tokio::test]
    async fn calls_api_rejects_inbound_and_unsupported_transport() {
        let interview = Uuid::new_v4();
        let state = test_state(workflow());
        let (status, _) = call_request(
            state.clone(),
            "POST",
            "/calls",
            Some(json!({
                "provider": "twilio",
                "interview_session_id": interview,
                "direction": "inbound",
                "transport": "pstn"
            })),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);

        let (status, _) = call_request(
            state.clone(),
            "POST",
            "/calls",
            Some(json!({
                "provider": "vapi",
                "interview_session_id": interview,
                "transport": "browser",
                "payload": {}
            })),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);

        let (status, body) = call_request(
            state,
            "GET",
            &format!("/calls?interview_session_id={interview}"),
            None,
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["calls"].as_array().unwrap().len(), 0);
    }

    #[tokio::test]
    async fn calls_api_fails_closed_when_the_workflow_is_disabled() {
        let mut state = test_state(workflow());
        state.calls = None;
        let (status, _) = call_request(state, "GET", "/calls/health", None).await;
        assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    }
}
