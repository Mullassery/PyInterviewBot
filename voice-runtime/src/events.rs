//! Normalized, sequenced voice-session events.

use serde::Serialize;
use uuid::Uuid;

use crate::state::VoiceState;

pub type SessionId = Uuid;

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum CompletionReason {
    ClientEnded,
    ProviderCompleted,
    Timeout,
    Error,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum VoiceEvent {
    SessionStarted {
        session_id: SessionId,
        sequence: u64,
        provider: String,
        state: VoiceState,
    },
    SessionStatus {
        session_id: SessionId,
        sequence: u64,
        state: VoiceState,
    },
    AudioAccepted {
        session_id: SessionId,
        sequence: u64,
        bytes: usize,
        total_bytes: usize,
    },
    SpeechStarted {
        session_id: SessionId,
        sequence: u64,
    },
    SpeechEnded {
        session_id: SessionId,
        sequence: u64,
    },
    SpeechInterrupted {
        session_id: SessionId,
        sequence: u64,
    },
    Transcript {
        session_id: SessionId,
        sequence: u64,
        turn: u32,
        text: String,
        is_final: bool,
        confidence: Option<f32>,
    },
    AudioChunk {
        session_id: SessionId,
        sequence: u64,
        pcm16: Vec<u8>,
    },
    ToolRequest {
        session_id: SessionId,
        sequence: u64,
        name: String,
        arguments: serde_json::Value,
    },
    ProviderError {
        session_id: SessionId,
        sequence: u64,
        provider: String,
        code: String,
        message: String,
        retryable: bool,
    },
    SessionCompleted {
        session_id: SessionId,
        sequence: u64,
        reason: CompletionReason,
    },
    SessionTimedOut {
        session_id: SessionId,
        sequence: u64,
    },
}

impl VoiceEvent {
    pub fn sequence(&self) -> u64 {
        match self {
            Self::SessionStarted { sequence, .. }
            | Self::SessionStatus { sequence, .. }
            | Self::AudioAccepted { sequence, .. }
            | Self::SpeechStarted { sequence, .. }
            | Self::SpeechEnded { sequence, .. }
            | Self::SpeechInterrupted { sequence, .. }
            | Self::Transcript { sequence, .. }
            | Self::AudioChunk { sequence, .. }
            | Self::ToolRequest { sequence, .. }
            | Self::ProviderError { sequence, .. }
            | Self::SessionCompleted { sequence, .. }
            | Self::SessionTimedOut { sequence, .. } => *sequence,
        }
    }

    pub fn session_id(&self) -> SessionId {
        match self {
            Self::SessionStarted { session_id, .. }
            | Self::SessionStatus { session_id, .. }
            | Self::AudioAccepted { session_id, .. }
            | Self::SpeechStarted { session_id, .. }
            | Self::SpeechEnded { session_id, .. }
            | Self::SpeechInterrupted { session_id, .. }
            | Self::Transcript { session_id, .. }
            | Self::AudioChunk { session_id, .. }
            | Self::ToolRequest { session_id, .. }
            | Self::ProviderError { session_id, .. }
            | Self::SessionCompleted { session_id, .. }
            | Self::SessionTimedOut { session_id, .. } => *session_id,
        }
    }
}
