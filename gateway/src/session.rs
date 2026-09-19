//! Per-connection session: transport-layer state only. Domain data
//! (evidence, competencies, claims, transcript) lives in ai-service.

use uuid::Uuid;

use crate::state_machine::SessionState;
use crate::vad::TurnDetector;

pub struct Session {
    pub id: Uuid,
    pub ai_session_id: Uuid,
    pub state: SessionState,
    pub vad: TurnDetector,
    /// Raw PCM16LE 16kHz mono bytes accumulated for the turn currently being
    /// spoken by the candidate.
    pub utterance_buf: Vec<u8>,
    pub turn_number: u32,
}

impl Session {
    pub fn new(ai_session_id: Uuid) -> Self {
        Self {
            id: Uuid::new_v4(),
            ai_session_id,
            state: SessionState::Initializing,
            vad: TurnDetector::default(),
            utterance_buf: Vec::new(),
            turn_number: 0,
        }
    }
}
