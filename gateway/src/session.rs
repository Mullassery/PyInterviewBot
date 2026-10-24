//! Per-connection session: transport-layer state only. Domain data
//! (evidence, competencies, claims, transcript) lives in ai-service.

use uuid::Uuid;

use crate::state_machine::SessionState;
use crate::vad::{TurnDetector, DEFAULT_SILENCE_MS_TO_END_TURN};

/// ~100 seconds of 16kHz mono PCM16. This bounds one candidate turn so a
/// malicious or buggy client cannot grow memory without limit.
pub const DEFAULT_MAX_UTTERANCE_BYTES: usize = 3_200_000;

#[derive(Debug, Clone, Copy)]
pub struct SessionConfig {
    pub silence_ms: u32,
    pub max_utterance_bytes: usize,
}

impl Default for SessionConfig {
    fn default() -> Self {
        Self {
            silence_ms: DEFAULT_SILENCE_MS_TO_END_TURN,
            max_utterance_bytes: DEFAULT_MAX_UTTERANCE_BYTES,
        }
    }
}

pub struct Session {
    pub id: Uuid,
    pub ai_session_id: Uuid,
    pub state: SessionState,
    pub vad: TurnDetector,
    /// Raw PCM16LE 16kHz mono bytes accumulated for the turn currently being
    /// spoken by the candidate.
    pub utterance_buf: Vec<u8>,
    pub turn_number: u32,
    pub max_utterance_bytes: usize,
    pub ai_session_token: Option<String>,
}

impl Session {
    pub fn with_config(ai_session_id: Uuid, config: SessionConfig) -> Self {
        Self {
            id: Uuid::new_v4(),
            ai_session_id,
            state: SessionState::Initializing,
            vad: TurnDetector::with_silence_ms(config.silence_ms),
            utterance_buf: Vec::new(),
            turn_number: 0,
            max_utterance_bytes: config.max_utterance_bytes,
            ai_session_token: None,
        }
    }

    pub fn utterance_limit_exceeded(&self) -> bool {
        self.utterance_buf.len() > self.max_utterance_bytes
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_session_has_bounded_utterance() {
        let session = Session::with_config(Uuid::new_v4(), SessionConfig::default());
        assert_eq!(session.max_utterance_bytes, DEFAULT_MAX_UTTERANCE_BYTES);
    }

    #[test]
    fn custom_session_config_is_applied() {
        let session = Session::with_config(
            Uuid::new_v4(),
            SessionConfig {
                silence_ms: 1_200,
                max_utterance_bytes: 8,
            },
        );
        assert_eq!(session.vad.silence_ms_to_end_turn, 1_200);
        assert!(!session.utterance_limit_exceeded());
    }
}
