//! Deterministic interview session state machine (spec §32).
//!
//! This is intentionally kept free of any async/IO/LLM concerns so that every
//! legal and illegal transition can be unit tested in isolation, per spec §34
//! ("critical state must not exist only inside the LLM context").

use std::fmt;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum SessionState {
    Initializing,
    AiSpeaking,
    Listening,
    CandidateSpeaking,
    Processing,
    FollowUpDecision,
    AiResponding,
    Interrupted,
    Paused,
    Completed,
    ErrorRecovery,
}

impl fmt::Display for SessionState {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{:?}", self)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Event {
    /// AI has finished fetching/generating the next thing to say and has
    /// started streaming TTS audio to the candidate.
    StartSpeaking,
    /// TTS finished streaming with no interruption.
    FinishedSpeaking,
    /// VAD detected the candidate has started talking.
    CandidateVoiceDetected,
    /// VAD's end-of-turn heuristic fired (sustained silence after speech).
    CandidateTurnEnded,
    /// Buffered audio has been handed off to ai-service (ASR + agent turn).
    /// Not yet emitted by ws_handler.rs — see ROADMAP_HONEST.md.
    #[allow(dead_code)]
    ProcessingStarted,
    /// ai-service returned a decision on what to do next.
    TurnDecided,
    /// The agent decided the interview is over.
    InterviewComplete,
    /// Candidate spoke while the AI was mid-sentence.
    BargeIn,
    /// Candidate or operator paused the session. No UI control or code path
    /// triggers this yet — see ROADMAP_HONEST.md.
    #[allow(dead_code)]
    Pause,
    /// Resume from a paused session. Same as `Pause`, unreachable today.
    #[allow(dead_code)]
    Resume,
    /// A recoverable failure occurred (ASR/agent/TTS call failed).
    Fault,
    /// Recovered from ERROR_RECOVERY back into the loop. ws_handler.rs uses
    /// `StartSpeaking` for spoken recovery instead — this silent variant is
    /// kept for a future non-spoken recovery path but isn't emitted yet.
    #[allow(dead_code)]
    Recovered,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct IllegalTransition {
    pub from: SessionState,
    pub event: Event,
}

impl fmt::Display for IllegalTransition {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "illegal event {:?} in state {}", self.event, self.from)
    }
}

impl std::error::Error for IllegalTransition {}

/// Applies `event` to `state`, returning the resulting state or an error if
/// the transition is not allowed. Pure function — no side effects.
pub fn transition(state: SessionState, event: Event) -> Result<SessionState, IllegalTransition> {
    use Event::*;
    use SessionState::*;

    let next = match (state, event) {
        (Initializing, StartSpeaking) => AiSpeaking,
        (Initializing, Fault) => ErrorRecovery,

        (AiSpeaking, FinishedSpeaking) => Listening,
        (AiSpeaking, BargeIn) => Interrupted,
        (AiSpeaking, Fault) => ErrorRecovery,
        (AiSpeaking, Pause) => Paused,

        (Listening, CandidateVoiceDetected) => CandidateSpeaking,
        (Listening, Pause) => Paused,
        (Listening, Fault) => ErrorRecovery,

        (CandidateSpeaking, CandidateTurnEnded) => Processing,
        (CandidateSpeaking, Pause) => Paused,
        (CandidateSpeaking, Fault) => ErrorRecovery,

        (Processing, ProcessingStarted) => Processing,
        (Processing, TurnDecided) => FollowUpDecision,
        (Processing, InterviewComplete) => Completed,
        (Processing, Fault) => ErrorRecovery,

        (FollowUpDecision, StartSpeaking) => AiResponding,
        (FollowUpDecision, InterviewComplete) => Completed,
        (FollowUpDecision, Fault) => ErrorRecovery,

        (AiResponding, FinishedSpeaking) => Listening,
        (AiResponding, BargeIn) => Interrupted,
        (AiResponding, Fault) => ErrorRecovery,
        (AiResponding, Pause) => Paused,
        // The closing remark is delivered as a normal AiResponding turn;
        // the interview ends once it's done (or interrupted — either way
        // there's nothing left to probe).
        (AiResponding, InterviewComplete) => Completed,

        (Interrupted, CandidateVoiceDetected) => CandidateSpeaking,

        (Paused, Resume) => Listening,

        (ErrorRecovery, Recovered) => Listening,
        (ErrorRecovery, StartSpeaking) => AiSpeaking,
        (ErrorRecovery, Fault) => ErrorRecovery,

        _ => return Err(IllegalTransition { from: state, event }),
    };

    Ok(next)
}

#[cfg(test)]
mod tests {
    use super::*;
    use Event::*;
    use SessionState::*;

    #[test]
    fn happy_path_single_turn() {
        let s = Initializing;
        let s = transition(s, StartSpeaking).unwrap();
        assert_eq!(s, AiSpeaking);
        let s = transition(s, FinishedSpeaking).unwrap();
        assert_eq!(s, Listening);
        let s = transition(s, CandidateVoiceDetected).unwrap();
        assert_eq!(s, CandidateSpeaking);
        let s = transition(s, CandidateTurnEnded).unwrap();
        assert_eq!(s, Processing);
        let s = transition(s, TurnDecided).unwrap();
        assert_eq!(s, FollowUpDecision);
        let s = transition(s, StartSpeaking).unwrap();
        assert_eq!(s, AiResponding);
        let s = transition(s, FinishedSpeaking).unwrap();
        assert_eq!(s, Listening);
    }

    #[test]
    fn barge_in_from_ai_speaking() {
        let s = transition(AiSpeaking, BargeIn).unwrap();
        assert_eq!(s, Interrupted);
        let s = transition(s, CandidateVoiceDetected).unwrap();
        assert_eq!(s, CandidateSpeaking);
    }

    #[test]
    fn barge_in_from_ai_responding() {
        let s = transition(AiResponding, BargeIn).unwrap();
        assert_eq!(s, Interrupted);
    }

    #[test]
    fn interview_completes_after_processing() {
        let s = transition(Processing, InterviewComplete).unwrap();
        assert_eq!(s, Completed);
    }

    #[test]
    fn interview_completes_after_followup_decision() {
        let s = transition(FollowUpDecision, InterviewComplete).unwrap();
        assert_eq!(s, Completed);
    }

    #[test]
    fn interview_completes_after_closing_remark_is_spoken() {
        let s = transition(FollowUpDecision, StartSpeaking).unwrap();
        assert_eq!(s, AiResponding);
        let s = transition(s, InterviewComplete).unwrap();
        assert_eq!(s, Completed);
    }

    #[test]
    fn fault_from_any_active_state_goes_to_error_recovery() {
        for s in [
            Initializing,
            AiSpeaking,
            Listening,
            CandidateSpeaking,
            Processing,
            FollowUpDecision,
            AiResponding,
        ] {
            assert_eq!(transition(s, Fault).unwrap(), ErrorRecovery);
        }
    }

    #[test]
    fn error_recovery_returns_to_listening() {
        let s = transition(ErrorRecovery, Recovered).unwrap();
        assert_eq!(s, Listening);
    }

    #[test]
    fn error_recovery_can_speak_a_fallback_phrase() {
        // A spoken recovery ("I didn't quite catch that...") re-enters the
        // normal AiSpeaking state so it gets barge-in support for free.
        let s = transition(ErrorRecovery, StartSpeaking).unwrap();
        assert_eq!(s, AiSpeaking);
        let s = transition(s, FinishedSpeaking).unwrap();
        assert_eq!(s, Listening);
    }

    #[test]
    fn pause_and_resume_round_trip() {
        let s = transition(Listening, Pause).unwrap();
        assert_eq!(s, Paused);
        let s = transition(s, Resume).unwrap();
        assert_eq!(s, Listening);
    }

    #[test]
    fn completed_state_accepts_no_further_events() {
        assert!(transition(Completed, StartSpeaking).is_err());
        assert!(transition(Completed, Fault).is_err());
    }

    #[test]
    fn cannot_skip_processing_straight_to_responding() {
        assert!(transition(CandidateSpeaking, StartSpeaking).is_err());
    }

    #[test]
    fn cannot_barge_in_while_listening() {
        // Barge-in is only meaningful while the AI itself is talking.
        assert!(transition(Listening, BargeIn).is_err());
    }

    #[test]
    fn illegal_transition_display_is_readable() {
        let err = transition(Completed, StartSpeaking).unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("StartSpeaking"));
        assert!(msg.contains("Completed"));
    }
}
