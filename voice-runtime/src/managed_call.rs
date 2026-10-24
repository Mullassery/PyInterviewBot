//! Native Rust managed-call provider integration.
//!
//! The upstream managed-call platform is proprietary and is not a self-hosted
//! speech engine. This module implements the server-side workflow required to
//! use such a provider safely: credentials stay server-side, webhooks are
//! authenticated, and events are normalized into PyInterviewBot voice contracts.

use std::collections::{HashMap, HashSet, VecDeque};
use std::fmt;
use std::sync::Mutex;
use std::time::Duration;

use hmac::{Hmac, Mac};
use reqwest::header::HeaderMap;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;
use thiserror::Error;

use crate::provider::ProviderEvent;
use crate::studio::StudioSessionId;

pub const DEFAULT_MAX_WEBHOOK_BYTES: usize = 1024 * 1024;
pub const DEFAULT_DEDUP_CAPACITY: usize = 4096;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WebhookAuth {
    Bearer {
        header: String,
        include_prefix: bool,
    },
    Hmac {
        header: String,
    },
}

#[derive(Clone)]
pub struct ManagedCallConfig {
    pub api_key: Option<String>,
    pub base_url: String,
    pub webhook_secret: Option<String>,
    pub webhook_auth: WebhookAuth,
    pub timeout: Duration,
    pub max_webhook_bytes: usize,
}

impl fmt::Debug for ManagedCallConfig {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ManagedCallConfig")
            .field("api_key", &self.api_key.as_ref().map(|_| "<redacted>"))
            .field("base_url", &self.base_url)
            .field(
                "webhook_secret",
                &self.webhook_secret.as_ref().map(|_| "<redacted>"),
            )
            .field("webhook_auth", &self.webhook_auth)
            .field("timeout", &self.timeout)
            .field("max_webhook_bytes", &self.max_webhook_bytes)
            .finish()
    }
}

impl Default for ManagedCallConfig {
    fn default() -> Self {
        Self {
            api_key: None,
            base_url: String::new(),
            webhook_secret: None,
            webhook_auth: WebhookAuth::Bearer {
                header: "Authorization".to_string(),
                include_prefix: true,
            },
            timeout: Duration::from_secs(30),
            max_webhook_bytes: DEFAULT_MAX_WEBHOOK_BYTES,
        }
    }
}

impl ManagedCallConfig {
    pub fn from_env() -> Self {
        let mode = std::env::var("MANAGED_CALL_WEBHOOK_AUTH_MODE")
            .unwrap_or_else(|_| "bearer".to_string())
            .to_lowercase();
        let webhook_auth = if mode == "hmac" {
            WebhookAuth::Hmac {
                header: std::env::var("MANAGED_CALL_WEBHOOK_SIGNATURE_HEADER")
                    .unwrap_or_else(|_| "X-Signature".to_string()),
            }
        } else {
            WebhookAuth::Bearer {
                header: std::env::var("MANAGED_CALL_WEBHOOK_SIGNATURE_HEADER")
                    .unwrap_or_else(|_| "Authorization".to_string()),
                include_prefix: true,
            }
        };
        Self {
            api_key: std::env::var("MANAGED_CALL_API_KEY")
                .ok()
                .filter(|key| !key.is_empty()),
            base_url: std::env::var("MANAGED_CALL_BASE_URL").unwrap_or_default(),
            webhook_secret: std::env::var("MANAGED_CALL_WEBHOOK_SECRET")
                .ok()
                .filter(|secret| !secret.is_empty()),
            webhook_auth,
            timeout: std::env::var("MANAGED_CALL_TIMEOUT_SECONDS")
                .ok()
                .and_then(|value| value.parse().ok())
                .map(Duration::from_secs)
                .unwrap_or(Duration::from_secs(30)),
            max_webhook_bytes: std::env::var("MANAGED_CALL_MAX_WEBHOOK_BYTES")
                .ok()
                .and_then(|value| value.parse().ok())
                .unwrap_or(DEFAULT_MAX_WEBHOOK_BYTES),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum ManagedCallError {
    #[error("managed call provider is not configured: {0}")]
    NotConfigured(String),
    #[error("invalid webhook: {0}")]
    InvalidWebhook(String),
    #[error("webhook payload exceeds the limit")]
    PayloadTooLarge,
    #[error("invalid managed call payload: {0}")]
    InvalidPayload(String),
    #[error("managed call request failed: {0}")]
    Request(String),
    #[error("managed call returned HTTP {status}: {body}")]
    Status {
        status: u16,
        body: String,
        retryable: bool,
    },
}

impl ManagedCallError {
    pub fn retryable(&self) -> bool {
        match self {
            Self::Status { retryable, .. } => *retryable,
            Self::Request(_) => true,
            _ => false,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ManagedCall {
    pub id: String,
    pub status: Option<String>,
    #[serde(flatten)]
    pub raw: Value,
}

#[derive(Clone)]
pub struct ManagedCallClient {
    http: reqwest::Client,
    config: ManagedCallConfig,
}

impl fmt::Debug for ManagedCallClient {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ManagedCallClient")
            .field("config", &self.config)
            .finish()
    }
}

impl ManagedCallClient {
    pub fn new(config: ManagedCallConfig) -> Result<Self, ManagedCallError> {
        if config
            .api_key
            .as_deref()
            .map(str::trim)
            .filter(|key| !key.is_empty())
            .is_none()
        {
            return Err(ManagedCallError::NotConfigured(
                "MANAGED_CALL_API_KEY is required".to_string(),
            ));
        }
        if config.base_url.trim().is_empty() {
            return Err(ManagedCallError::NotConfigured(
                "MANAGED_CALL_BASE_URL is required".to_string(),
            ));
        }
        let http = reqwest::Client::builder()
            .timeout(config.timeout)
            .build()
            .map_err(|err| ManagedCallError::Request(err.to_string()))?;
        Ok(Self { http, config })
    }

    pub fn base_url(&self) -> &str {
        &self.config.base_url
    }

    pub async fn create_call(&self, payload: Value) -> Result<ManagedCall, ManagedCallError> {
        self.send(reqwest::Method::POST, "/call", Some(payload))
            .await
    }

    pub async fn get_call(&self, call_id: &str) -> Result<ManagedCall, ManagedCallError> {
        self.send(
            reqwest::Method::GET,
            &format!("/call/{}", url_segment(call_id)),
            None,
        )
        .await
    }

    pub async fn end_call(&self, call_id: &str) -> Result<ManagedCall, ManagedCallError> {
        self.send(
            reqwest::Method::DELETE,
            &format!("/call/{}", url_segment(call_id)),
            None,
        )
        .await
    }

    async fn send(
        &self,
        method: reqwest::Method,
        path: &str,
        body: Option<Value>,
    ) -> Result<ManagedCall, ManagedCallError> {
        let key = self.config.api_key.as_deref().ok_or_else(|| {
            ManagedCallError::NotConfigured("MANAGED_CALL_API_KEY is required".to_string())
        })?;
        let url = format!("{}{}", self.config.base_url.trim_end_matches('/'), path);
        let mut builder = self
            .http
            .request(method, url)
            .header("Authorization", format!("Bearer {key}"));
        if let Some(body) = body {
            builder = builder.json(&body);
        }
        let response = builder
            .send()
            .await
            .map_err(|err| ManagedCallError::Request(err.to_string()))?;
        let status = response.status();
        let text = response
            .text()
            .await
            .map_err(|err| ManagedCallError::Request(err.to_string()))?;
        if !status.is_success() {
            return Err(ManagedCallError::Status {
                status: status.as_u16(),
                body: text.chars().take(500).collect(),
                retryable: status.as_u16() == 408
                    || status.as_u16() == 429
                    || status.is_server_error(),
            });
        }
        serde_json::from_str::<ManagedCall>(&text)
            .map_err(|err| ManagedCallError::InvalidPayload(format!("{err}: {text}")))
    }
}

fn url_segment(value: &str) -> String {
    value
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || ch == '-' || ch == '_' {
                ch
            } else {
                '_'
            }
        })
        .collect()
}

#[derive(Clone)]
pub struct WebhookVerifier {
    secret: Option<String>,
    auth: WebhookAuth,
}

impl fmt::Debug for WebhookVerifier {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("WebhookVerifier")
            .field("secret", &self.secret.as_ref().map(|_| "<redacted>"))
            .field("auth", &self.auth)
            .finish()
    }
}

impl WebhookVerifier {
    pub fn new(config: &ManagedCallConfig) -> Self {
        Self {
            secret: config.webhook_secret.clone(),
            auth: config.webhook_auth.clone(),
        }
    }

    pub fn verify(&self, headers: &HeaderMap, body: &[u8]) -> Result<(), ManagedCallError> {
        let secret = self
            .secret
            .as_deref()
            .filter(|secret| !secret.is_empty())
            .ok_or_else(|| {
                ManagedCallError::NotConfigured(
                    "MANAGED_CALL_WEBHOOK_SECRET is required".to_string(),
                )
            })?;
        match &self.auth {
            WebhookAuth::Bearer {
                header,
                include_prefix,
            } => {
                let supplied = header_value(headers, header).ok_or_else(|| {
                    ManagedCallError::InvalidWebhook(format!("missing {header} header"))
                })?;
                let expected = if *include_prefix {
                    format!("Bearer {secret}")
                } else {
                    secret.to_string()
                };
                let supplied = supplied.as_bytes();
                let expected = expected.as_bytes();
                if supplied.len() == expected.len() && supplied.ct_eq(expected).into() {
                    Ok(())
                } else {
                    Err(ManagedCallError::InvalidWebhook(
                        "bearer token mismatch".to_string(),
                    ))
                }
            }
            WebhookAuth::Hmac { header } => {
                let supplied = header_value(headers, header).ok_or_else(|| {
                    ManagedCallError::InvalidWebhook(format!("missing {header} header"))
                })?;
                let supplied = supplied.strip_prefix("sha256=").unwrap_or(&supplied);
                let signature = hex::decode(supplied).map_err(|_| {
                    ManagedCallError::InvalidWebhook("signature is not valid hex".to_string())
                })?;
                let mut mac = Hmac::<Sha256>::new_from_slice(secret.as_bytes()).map_err(|_| {
                    ManagedCallError::InvalidWebhook("invalid HMAC key".to_string())
                })?;
                mac.update(body);
                mac.verify_slice(&signature).map_err(|_| {
                    ManagedCallError::InvalidWebhook("HMAC signature mismatch".to_string())
                })
            }
        }
    }
}

fn header_value(headers: &HeaderMap, name: &str) -> Option<String> {
    headers
        .get(name)
        .and_then(|value| value.to_str().ok())
        .map(str::to_string)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ManagedEventType {
    SessionStatus,
    SpeechStarted,
    SpeechEnded,
    TranscriptPartial,
    TranscriptFinal,
    SessionCompleted,
    ToolCalls,
    AssistantRequested,
    ConversationUpdated,
    Error,
    Unknown,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct NormalizedManagedEvent {
    pub event_type: ManagedEventType,
    pub call_id: Option<String>,
    pub sequence: Option<u64>,
    pub dedup_key: String,
    pub data: Value,
}

impl NormalizedManagedEvent {
    pub fn to_provider_events(&self) -> Vec<ProviderEvent> {
        match self.event_type {
            ManagedEventType::SpeechStarted => vec![ProviderEvent::SpeechStarted],
            ManagedEventType::SpeechEnded => vec![ProviderEvent::SpeechEnded],
            ManagedEventType::TranscriptPartial => self
                .transcript_text()
                .map(ProviderEvent::TranscriptPartial)
                .into_iter()
                .collect(),
            ManagedEventType::TranscriptFinal => self
                .transcript_text()
                .map(|text| ProviderEvent::TranscriptFinal {
                    text,
                    confidence: None,
                })
                .into_iter()
                .collect(),
            ManagedEventType::SessionCompleted => vec![ProviderEvent::Completed],
            ManagedEventType::Error => vec![ProviderEvent::Error {
                code: self
                    .data
                    .get("error")
                    .and_then(|error| error.get("code"))
                    .and_then(Value::as_str)
                    .unwrap_or("managed_call_error")
                    .to_string(),
                message: self
                    .data
                    .get("error")
                    .and_then(|error| error.get("message"))
                    .and_then(Value::as_str)
                    .or_else(|| self.data.get("message").and_then(Value::as_str))
                    .unwrap_or("managed call provider error")
                    .to_string(),
                retryable: false,
            }],
            _ => Vec::new(),
        }
    }

    fn transcript_text(&self) -> Option<String> {
        self.data
            .get("transcript")
            .and_then(Value::as_str)
            .map(str::to_string)
            .filter(|text| !text.is_empty())
    }
}

pub fn normalize_managed_event(
    payload: &Value,
) -> Result<NormalizedManagedEvent, ManagedCallError> {
    let message = payload.get("message").unwrap_or(payload);
    let message = message
        .as_object()
        .ok_or_else(|| ManagedCallError::InvalidPayload("message is not an object".to_string()))?;
    let message_type = message
        .get("type")
        .and_then(Value::as_str)
        .ok_or_else(|| ManagedCallError::InvalidPayload("missing message.type".to_string()))?;

    let event_type = match message_type {
        "status-update" => ManagedEventType::SessionStatus,
        "speech-update" => {
            if message
                .get("status")
                .and_then(Value::as_str)
                .is_some_and(|status| status.eq_ignore_ascii_case("started"))
            {
                ManagedEventType::SpeechStarted
            } else {
                ManagedEventType::SpeechEnded
            }
        }
        "transcript" => {
            if message
                .get("transcriptType")
                .and_then(Value::as_str)
                .is_some_and(|kind| kind.eq_ignore_ascii_case("final"))
            {
                ManagedEventType::TranscriptFinal
            } else {
                ManagedEventType::TranscriptPartial
            }
        }
        "end-of-call-report" => ManagedEventType::SessionCompleted,
        "tool-calls" => ManagedEventType::ToolCalls,
        "assistant-request" => ManagedEventType::AssistantRequested,
        "conversation-update" => ManagedEventType::ConversationUpdated,
        "error" => ManagedEventType::Error,
        _ => ManagedEventType::Unknown,
    };

    let call_id = message
        .get("call")
        .and_then(|call| call.get("id"))
        .or_else(|| message.get("callId"))
        .and_then(Value::as_str)
        .map(str::to_string);
    let sequence = message
        .get("sequenceNumber")
        .or_else(|| message.get("sequence"))
        .and_then(Value::as_u64);

    let explicit_id = message
        .get("id")
        .or_else(|| message.get("eventId"))
        .and_then(Value::as_str);
    let dedup_key = explicit_id.map(str::to_string).unwrap_or_else(|| {
        let mut hasher = Sha256::new();
        hasher.update(payload.to_string().as_bytes());
        hex::encode(hasher.finalize())
    });

    Ok(NormalizedManagedEvent {
        event_type,
        call_id,
        sequence,
        dedup_key,
        data: Value::Object(message.clone()),
    })
}

struct ManagedSession {
    call_id: String,
    queue: VecDeque<ProviderEvent>,
}

struct BoundedSeen {
    order: VecDeque<String>,
    set: HashSet<String>,
    capacity: usize,
}

impl BoundedSeen {
    fn new(capacity: usize) -> Self {
        Self {
            order: VecDeque::new(),
            set: HashSet::new(),
            capacity: capacity.max(1),
        }
    }

    fn insert(&mut self, key: String) -> bool {
        if self.set.contains(&key) {
            return false;
        }
        self.set.insert(key.clone());
        self.order.push_back(key);
        while self.order.len() > self.capacity {
            if let Some(old) = self.order.pop_front() {
                self.set.remove(&old);
            }
        }
        true
    }
}

pub struct ManagedCallWorkflow {
    pub client: ManagedCallClient,
    pub verifier: WebhookVerifier,
    sessions: Mutex<HashMap<StudioSessionId, ManagedSession>>,
    seen: Mutex<BoundedSeen>,
    max_webhook_bytes: usize,
}

impl ManagedCallWorkflow {
    pub fn new(
        client: ManagedCallClient,
        verifier: WebhookVerifier,
        max_webhook_bytes: usize,
    ) -> Self {
        Self {
            client,
            verifier,
            sessions: Mutex::new(HashMap::new()),
            seen: Mutex::new(BoundedSeen::new(DEFAULT_DEDUP_CAPACITY)),
            max_webhook_bytes,
        }
    }

    pub async fn start_call(
        &self,
        session_id: StudioSessionId,
        payload: Value,
    ) -> Result<ManagedCall, ManagedCallError> {
        let call = self.client.create_call(payload).await?;
        let mut sessions = self
            .sessions
            .lock()
            .expect("managed call session lock poisoned");
        sessions.insert(
            session_id,
            ManagedSession {
                call_id: call.id.clone(),
                queue: VecDeque::new(),
            },
        );
        Ok(call)
    }

    pub fn call_id(&self, session_id: StudioSessionId) -> Option<String> {
        self.sessions
            .lock()
            .expect("managed call session lock poisoned")
            .get(&session_id)
            .map(|session| session.call_id.clone())
    }

    pub async fn end_call(&self, session_id: StudioSessionId) -> Result<(), ManagedCallError> {
        let call_id = {
            let mut sessions = self
                .sessions
                .lock()
                .expect("managed call session lock poisoned");
            sessions.remove(&session_id).map(|session| session.call_id)
        }
        .ok_or_else(|| {
            ManagedCallError::NotConfigured("unknown managed call session".to_string())
        })?;
        self.client.end_call(&call_id).await.map(|_| ())
    }

    pub fn ingest_webhook(
        &self,
        headers: &HeaderMap,
        body: &[u8],
    ) -> Result<Vec<NormalizedManagedEvent>, ManagedCallError> {
        if body.len() > self.max_webhook_bytes {
            return Err(ManagedCallError::PayloadTooLarge);
        }
        self.verifier.verify(headers, body)?;
        let payload: Value = serde_json::from_slice(body)
            .map_err(|err| ManagedCallError::InvalidPayload(err.to_string()))?;
        let event = normalize_managed_event(&payload)?;

        {
            let mut seen = self.seen.lock().expect("managed call dedup lock poisoned");
            if !seen.insert(event.dedup_key.clone()) {
                return Ok(Vec::new());
            }
        }

        if let Some(call_id) = &event.call_id {
            let provider_events = event.to_provider_events();
            if !provider_events.is_empty() {
                let mut sessions = self
                    .sessions
                    .lock()
                    .expect("managed call session lock poisoned");
                if let Some(session) = sessions
                    .values_mut()
                    .find(|session| &session.call_id == call_id)
                {
                    session.queue.extend(provider_events);
                }
            }
        }
        Ok(vec![event])
    }

    pub fn drain_provider_events(&self, session_id: StudioSessionId) -> Vec<ProviderEvent> {
        self.sessions
            .lock()
            .expect("managed call session lock poisoned")
            .get_mut(&session_id)
            .map(|session| session.queue.drain(..).collect())
            .unwrap_or_default()
    }

    pub fn seen_len(&self) -> usize {
        self.seen
            .lock()
            .expect("managed call dedup lock poisoned")
            .set
            .len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{
        extract::State,
        http::StatusCode,
        routing::{get, post},
        Json, Router,
    };
    use serde_json::json;
    use std::sync::Arc;
    use tokio::sync::Mutex as AsyncMutex;
    use uuid::Uuid;

    #[test]
    fn normalizes_documented_events() {
        let partial = normalize_managed_event(&json!({
            "message": {
                "type": "transcript",
                "transcriptType": "partial",
                "transcript": "hello",
                "call": {"id": "call-1"},
                "sequenceNumber": 4
            }
        }))
        .unwrap();
        assert_eq!(partial.event_type, ManagedEventType::TranscriptPartial);
        assert_eq!(partial.call_id.as_deref(), Some("call-1"));
        assert_eq!(partial.sequence, Some(4));
        assert_eq!(
            partial.to_provider_events(),
            vec![ProviderEvent::TranscriptPartial("hello".to_string())]
        );

        let completed = normalize_managed_event(&json!({
            "message": {"type": "end-of-call-report", "call": {"id": "call-1"}}
        }))
        .unwrap();
        assert_eq!(completed.event_type, ManagedEventType::SessionCompleted);
        assert_eq!(
            completed.to_provider_events(),
            vec![ProviderEvent::Completed]
        );

        let unknown = normalize_managed_event(&json!({"message": {"type": "future"}})).unwrap();
        assert_eq!(unknown.event_type, ManagedEventType::Unknown);
    }

    #[test]
    fn verifies_bearer_and_hmac_webhooks() {
        let bearer = ManagedCallConfig {
            webhook_secret: Some("secret".to_string()),
            ..ManagedCallConfig::default()
        };
        let verifier = WebhookVerifier::new(&bearer);
        let mut headers = HeaderMap::new();
        headers.insert("Authorization", "Bearer secret".parse().unwrap());
        assert!(verifier.verify(&headers, b"{}").is_ok());
        headers.insert("Authorization", "Bearer wrong".parse().unwrap());
        assert!(verifier.verify(&headers, b"{}").is_err());

        let hmac_config = ManagedCallConfig {
            webhook_secret: Some("secret".to_string()),
            webhook_auth: WebhookAuth::Hmac {
                header: "X-Signature".to_string(),
            },
            ..ManagedCallConfig::default()
        };
        let verifier = WebhookVerifier::new(&hmac_config);
        let body = br#"{"hello":"world"}"#;
        let mut mac = Hmac::<Sha256>::new_from_slice(b"secret").unwrap();
        mac.update(body);
        let signature = hex::encode(mac.finalize().into_bytes());
        let mut headers = HeaderMap::new();
        headers.insert("X-Signature", signature.parse().unwrap());
        assert!(verifier.verify(&headers, body).is_ok());
    }

    #[test]
    fn verifier_fails_closed_without_secret() {
        let config = ManagedCallConfig {
            webhook_secret: None,
            ..ManagedCallConfig::default()
        };
        let verifier = WebhookVerifier::new(&config);
        assert!(verifier.verify(&HeaderMap::new(), b"{}").is_err());
    }

    async fn mock_managed_provider() -> (String, Arc<AsyncMutex<Vec<String>>>) {
        let requests = Arc::new(AsyncMutex::new(Vec::new()));
        async fn call(
            State(requests): State<Arc<AsyncMutex<Vec<String>>>>,
            headers: HeaderMap,
            Json(payload): Json<Value>,
        ) -> Json<Value> {
            requests.lock().await.push(format!(
                "POST {} {}",
                headers
                    .get("authorization")
                    .and_then(|v| v.to_str().ok())
                    .unwrap_or(""),
                payload
            ));
            Json(json!({"id": "call-1", "status": "queued"}))
        }
        async fn get_call(State(requests): State<Arc<AsyncMutex<Vec<String>>>>) -> Json<Value> {
            requests.lock().await.push("GET".to_string());
            Json(json!({"id": "call-1", "status": "in-progress"}))
        }
        async fn delete_call(
            State(requests): State<Arc<AsyncMutex<Vec<String>>>>,
        ) -> (StatusCode, Json<Value>) {
            requests.lock().await.push("DELETE".to_string());
            (
                StatusCode::OK,
                Json(json!({"id": "call-1", "status": "ended"})),
            )
        }
        let app = Router::new()
            .route("/call", post(call))
            .route("/call/{id}", get(get_call).delete(delete_call))
            .with_state(requests.clone());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        (format!("http://{address}"), requests)
    }

    #[tokio::test]
    async fn client_creates_gets_and_ends_calls_server_side() {
        let (base_url, requests) = mock_managed_provider().await;
        let client = ManagedCallClient::new(ManagedCallConfig {
            api_key: Some("private-key".to_string()),
            base_url,
            ..ManagedCallConfig::default()
        })
        .unwrap();
        let call = client
            .create_call(json!({"assistantId": "a-1"}))
            .await
            .unwrap();
        assert_eq!(call.id, "call-1");
        let fetched = client.get_call("call-1").await.unwrap();
        assert_eq!(fetched.status.as_deref(), Some("in-progress"));
        client.end_call("call-1").await.unwrap();
        let requests = requests.lock().await;
        assert!(requests[0].contains("Bearer private-key"));
        assert_eq!(requests[1], "GET");
        assert_eq!(requests[2], "DELETE");
    }

    #[tokio::test]
    async fn workflow_is_idempotent_and_drains_provider_events() {
        let (base_url, _requests) = mock_managed_provider().await;
        let config = ManagedCallConfig {
            api_key: Some("key".to_string()),
            base_url,
            webhook_secret: Some("secret".to_string()),
            ..ManagedCallConfig::default()
        };
        let client = ManagedCallClient::new(config.clone()).unwrap();
        let workflow = ManagedCallWorkflow::new(
            client,
            WebhookVerifier::new(&config),
            DEFAULT_MAX_WEBHOOK_BYTES,
        );
        let session = Uuid::new_v4();
        workflow
            .start_call(session, json!({"assistantId": "a-1"}))
            .await
            .unwrap();
        assert_eq!(workflow.call_id(session).as_deref(), Some("call-1"));

        let body = br#"{"message":{"type":"transcript","transcriptType":"final","transcript":"answer","call":{"id":"call-1"}}}"#;
        let mut headers = HeaderMap::new();
        headers.insert("Authorization", "Bearer secret".parse().unwrap());
        let first = workflow.ingest_webhook(&headers, body).unwrap();
        assert_eq!(first.len(), 1);
        let duplicate = workflow.ingest_webhook(&headers, body).unwrap();
        assert!(duplicate.is_empty());
        assert_eq!(workflow.seen_len(), 1);

        let drained = workflow.drain_provider_events(session);
        assert_eq!(
            drained,
            vec![ProviderEvent::TranscriptFinal {
                text: "answer".to_string(),
                confidence: None,
            }]
        );
        assert!(workflow.drain_provider_events(session).is_empty());
    }

    #[tokio::test]
    async fn workflow_rejects_oversized_or_unauthenticated_webhooks() {
        let (base_url, _requests) = mock_managed_provider().await;
        let config = ManagedCallConfig {
            api_key: Some("key".to_string()),
            base_url,
            webhook_secret: Some("secret".to_string()),
            ..ManagedCallConfig::default()
        };
        let workflow = ManagedCallWorkflow::new(
            ManagedCallClient::new(config.clone()).unwrap(),
            WebhookVerifier::new(&config),
            8,
        );
        let mut headers = HeaderMap::new();
        headers.insert("Authorization", "Bearer secret".parse().unwrap());
        assert!(matches!(
            workflow.ingest_webhook(&headers, b"123456789"),
            Err(ManagedCallError::PayloadTooLarge)
        ));
        assert!(matches!(
            workflow.ingest_webhook(&HeaderMap::new(), b"{}"),
            Err(ManagedCallError::InvalidWebhook(_))
        ));
    }
}
