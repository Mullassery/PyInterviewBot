//! Provider-neutral voice contracts. Interview state stays in the runtime,
//! never in a speech provider.

use std::future::Future;
use std::pin::Pin;

use serde::Serialize;
use thiserror::Error;
use uuid::Uuid;

pub type SessionId = Uuid;
pub type ProviderFuture<'a, T> =
    Pin<Box<dyn Future<Output = Result<T, ProviderError>> + Send + 'a>>;

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ProviderCapabilities {
    pub name: String,
    pub local: bool,
    pub streaming_asr: bool,
    pub streaming_tts: bool,
    pub languages: Vec<String>,
    pub max_text_bytes: usize,
}

#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum ProviderError {
    #[error("provider unavailable: {0}")]
    Unavailable(String),
    #[error("provider timed out")]
    Timeout,
    #[error("invalid provider input: {0}")]
    InvalidInput(String),
    #[error("provider operation cancelled")]
    Cancelled,
    #[error("provider internal error: {0}")]
    Internal(String),
}

impl ProviderError {
    pub fn retryable(&self) -> bool {
        matches!(self, Self::Unavailable(_) | Self::Timeout)
    }

    pub fn code(&self) -> &'static str {
        match self {
            Self::Unavailable(_) => "unavailable",
            Self::Timeout => "timeout",
            Self::InvalidInput(_) => "invalid_input",
            Self::Cancelled => "cancelled",
            Self::Internal(_) => "internal",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ProviderEvent {
    Ready,
    SpeechStarted,
    SpeechEnded,
    SpeechInterrupted,
    TranscriptPartial(String),
    TranscriptFinal {
        text: String,
        confidence: Option<f32>,
    },
    Audio(Vec<u8>),
    ToolRequest {
        name: String,
        arguments: serde_json::Value,
    },
    Error {
        code: String,
        message: String,
        retryable: bool,
    },
    Completed,
}

#[derive(Debug, Clone, Serialize)]
pub struct SessionConfig {
    pub template_id: Option<String>,
    pub language: String,
}

impl Default for SessionConfig {
    fn default() -> Self {
        Self {
            template_id: None,
            language: "en".to_string(),
        }
    }
}

pub trait VoiceProvider: Send + Sync + 'static {
    fn capabilities(&self) -> ProviderCapabilities;

    fn health(&self) -> ProviderFuture<'_, bool>;

    fn start_session(
        &self,
        session_id: SessionId,
        config: &SessionConfig,
    ) -> ProviderFuture<'_, ()>;

    fn send_audio(&self, session_id: SessionId, pcm16: Vec<u8>) -> ProviderFuture<'_, ()>;

    fn speak(&self, session_id: SessionId, text: String) -> ProviderFuture<'_, ()>;

    fn cancel_speech(&self, session_id: SessionId) -> ProviderFuture<'_, ()>;

    fn end_session(&self, session_id: SessionId) -> ProviderFuture<'_, ()>;

    fn poll_event(&self, session_id: SessionId) -> ProviderFuture<'_, Option<ProviderEvent>>;
}
