//! Deterministic voice-session lifecycle. Pure state transitions, no IO.

use std::fmt;

use serde::Serialize;
use thiserror::Error;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum VoiceState {
    Created,
    Listening,
    Speaking,
    Interrupted,
    Closed,
    Failed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VoiceTransition {
    StartListening,
    StartSpeaking,
    FinishSpeaking,
    Interrupt,
    Close,
    Fail,
    Resume,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Error)]
pub struct IllegalVoiceTransition {
    pub from: VoiceState,
    pub transition: VoiceTransition,
}

impl fmt::Display for IllegalVoiceTransition {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "illegal voice transition {:?} from {:?}",
            self.transition, self.from
        )
    }
}

pub fn transition(
    state: VoiceState,
    event: VoiceTransition,
) -> Result<VoiceState, IllegalVoiceTransition> {
    use VoiceState::*;
    use VoiceTransition::*;

    let next = match (state, event) {
        (_, Close) if state != Closed => Closed,
        (_, Fail) if state != Closed => Failed,

        (Created, StartListening) => Listening,
        (Interrupted, StartListening) => Listening,
        (Failed, Resume) => Listening,
        (Interrupted, Resume) => Listening,

        (Created, StartSpeaking) => Speaking,
        (Listening, StartSpeaking) => Speaking,
        (Interrupted, StartSpeaking) => Speaking,

        (Speaking, FinishSpeaking) => Listening,
        (Speaking, Interrupt) => Interrupted,

        _ => {
            return Err(IllegalVoiceTransition {
                from: state,
                transition: event,
            })
        }
    };

    Ok(next)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn happy_path_round_trip() {
        let s = transition(VoiceState::Created, VoiceTransition::StartListening).unwrap();
        assert_eq!(s, VoiceState::Listening);
        let s = transition(s, VoiceTransition::StartSpeaking).unwrap();
        assert_eq!(s, VoiceState::Speaking);
        let s = transition(s, VoiceTransition::FinishSpeaking).unwrap();
        assert_eq!(s, VoiceState::Listening);
    }

    #[test]
    fn interrupt_requires_speaking() {
        assert!(transition(VoiceState::Listening, VoiceTransition::Interrupt).is_err());
        let s = transition(VoiceState::Speaking, VoiceTransition::Interrupt).unwrap();
        assert_eq!(s, VoiceState::Interrupted);
        assert_eq!(
            transition(s, VoiceTransition::Resume).unwrap(),
            VoiceState::Listening
        );
    }

    #[test]
    fn closed_is_terminal() {
        for event in [
            VoiceTransition::StartListening,
            VoiceTransition::StartSpeaking,
            VoiceTransition::Interrupt,
            VoiceTransition::Close,
            VoiceTransition::Fail,
            VoiceTransition::Resume,
        ] {
            assert!(transition(VoiceState::Closed, event).is_err());
        }
    }

    #[test]
    fn failure_can_recover() {
        let s = transition(VoiceState::Listening, VoiceTransition::Fail).unwrap();
        assert_eq!(s, VoiceState::Failed);
        assert_eq!(
            transition(s, VoiceTransition::Resume).unwrap(),
            VoiceState::Listening
        );
    }
}
