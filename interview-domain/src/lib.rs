//! Channel-independent interview contracts shared by provider adapters.
//!
//! Provider payloads are normalized before they reach this domain. The domain
//! does not know Twilio, WhatsApp, or Teams wire formats.

use std::collections::{HashSet, VecDeque};
use std::fmt;

use serde::{Deserialize, Serialize};
use thiserror::Error;
use uuid::Uuid;

pub mod call;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Channel {
    Web,
    TwilioVoice,
    WhatsAppText,
    WhatsAppVoiceNote,
    Teams,
}

impl Channel {
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "web" => Some(Self::Web),
            "twilio_voice" => Some(Self::TwilioVoice),
            "whatsapp_text" => Some(Self::WhatsAppText),
            "whatsapp_voice_note" => Some(Self::WhatsAppVoiceNote),
            "teams" => Some(Self::Teams),
            _ => None,
        }
    }
}

impl fmt::Display for Channel {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let value = match self {
            Self::Web => "web",
            Self::TwilioVoice => "twilio_voice",
            Self::WhatsAppText => "whatsapp_text",
            Self::WhatsAppVoiceNote => "whatsapp_voice_note",
            Self::Teams => "teams",
        };
        f.write_str(value)
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EventEnvelope {
    pub schema_version: u16,
    pub channel: Channel,
    pub external_id: String,
    pub correlation_id: String,
    pub session_id: Option<Uuid>,
    pub timestamp_ms: u64,
    pub payload: InboundEvent,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum InboundEvent {
    SessionStarted {
        template_id: Option<String>,
    },
    TextReceived {
        event_id: String,
        text: String,
    },
    AudioReceived {
        event_id: String,
        media_ref: String,
        content_type: Option<String>,
    },
    CallStatus {
        event_id: String,
        status: String,
    },
    MeetingTranscriptAvailable {
        event_id: String,
        transcript_ref: String,
    },
    ConsentRecorded {
        event_id: String,
        granted: bool,
    },
}

impl InboundEvent {
    pub fn event_id(&self) -> Option<&str> {
        match self {
            Self::TextReceived { event_id, .. }
            | Self::AudioReceived { event_id, .. }
            | Self::CallStatus { event_id, .. }
            | Self::MeetingTranscriptAvailable { event_id, .. }
            | Self::ConsentRecorded { event_id, .. } => Some(event_id),
            Self::SessionStarted { .. } => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum OutboundAction {
    AskQuestion { text: String },
    SendTextMessage { text: String },
    PlayAudio { media_ref: String },
    EndInterview { reason: String },
    RequestHumanReview { reason: String },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SessionState {
    Created,
    AwaitingCandidate,
    ProcessingResponse,
    AwaitingFollowUp,
    Completed,
    Cancelled,
    Failed,
    RequiresHumanReview,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionTransition {
    Start,
    CandidateResponded,
    ResponseProcessed,
    AskFollowUp,
    Complete,
    Cancel,
    Fail,
    RequireReview,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Error)]
#[error("illegal channel transition {transition:?} from {from:?}")]
pub struct IllegalTransition {
    pub from: SessionState,
    pub transition: SessionTransition,
}

pub fn transition(
    state: SessionState,
    event: SessionTransition,
) -> Result<SessionState, IllegalTransition> {
    use SessionState::*;
    use SessionTransition::*;
    let next = match (state, event) {
        (Created, Start) | (AwaitingFollowUp, Start) => AwaitingCandidate,
        (AwaitingCandidate, CandidateResponded) => ProcessingResponse,
        (ProcessingResponse, ResponseProcessed) => AwaitingFollowUp,
        (AwaitingFollowUp, AskFollowUp) => AwaitingCandidate,
        (AwaitingCandidate, Complete)
        | (ProcessingResponse, Complete)
        | (AwaitingFollowUp, Complete) => Completed,
        (_, Cancel) if state != Completed && state != Cancelled => Cancelled,
        (_, Fail) if state != Completed && state != Cancelled && state != Failed => Failed,
        (_, RequireReview) if !matches!(state, Completed | Cancelled | RequiresHumanReview) => {
            RequiresHumanReview
        }
        _ => {
            return Err(IllegalTransition {
                from: state,
                transition: event,
            })
        }
    };
    Ok(next)
}

#[derive(Debug, Clone)]
pub struct EventDedup {
    seen: HashSet<String>,
    order: VecDeque<String>,
    capacity: usize,
}

impl EventDedup {
    pub fn new(capacity: usize) -> Self {
        Self {
            seen: HashSet::new(),
            order: VecDeque::new(),
            capacity: capacity.max(1),
        }
    }

    /// Returns true if this is the first observation of the key.
    pub fn observe(&mut self, key: impl Into<String>) -> bool {
        let key = key.into();
        if !self.seen.insert(key.clone()) {
            return false;
        }
        self.order.push_back(key);
        while self.order.len() > self.capacity {
            if let Some(old) = self.order.pop_front() {
                self.seen.remove(&old);
            }
        }
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn channel_parsing_is_strict() {
        assert_eq!(Channel::parse("twilio_voice"), Some(Channel::TwilioVoice));
        assert_eq!(
            Channel::parse("whatsapp_voice_note"),
            Some(Channel::WhatsAppVoiceNote)
        );
        assert_eq!(Channel::parse("carrier_pigeon"), None);
        assert_eq!(Channel::TwilioVoice.to_string(), "twilio_voice");
    }

    #[test]
    fn same_logical_event_normalizes_identically_across_channels() {
        let base = InboundEvent::TextReceived {
            event_id: "evt-1".to_string(),
            text: "I owned the design.".to_string(),
        };
        let twilio = EventEnvelope {
            schema_version: 1,
            channel: Channel::TwilioVoice,
            external_id: "call-1".to_string(),
            correlation_id: "corr-1".to_string(),
            session_id: Some(Uuid::new_v4()),
            timestamp_ms: 1,
            payload: base.clone(),
        };
        let whatsapp = EventEnvelope {
            schema_version: 1,
            channel: Channel::WhatsAppText,
            external_id: "wa-1".to_string(),
            correlation_id: "corr-1".to_string(),
            session_id: twilio.session_id,
            timestamp_ms: 1,
            payload: base,
        };
        assert_eq!(twilio.payload, whatsapp.payload);
        assert_ne!(twilio.channel, whatsapp.channel);
    }

    #[test]
    fn valid_state_machine_sequence() {
        let state = transition(SessionState::Created, SessionTransition::Start).unwrap();
        let state = transition(state, SessionTransition::CandidateResponded).unwrap();
        assert_eq!(state, SessionState::ProcessingResponse);
        let state = transition(state, SessionTransition::ResponseProcessed).unwrap();
        let state = transition(state, SessionTransition::AskFollowUp).unwrap();
        assert_eq!(state, SessionState::AwaitingCandidate);
        let state = transition(state, SessionTransition::Complete).unwrap();
        assert_eq!(state, SessionState::Completed);
        assert!(transition(state, SessionTransition::Start).is_err());
    }

    #[test]
    fn dedup_rejects_duplicates_and_is_bounded() {
        let mut dedup = EventDedup::new(2);
        assert!(dedup.observe("a"));
        assert!(!dedup.observe("a"));
        assert!(dedup.observe("b"));
        assert!(dedup.observe("c"));
        assert!(dedup.observe("a"), "oldest key was evicted");
    }

    #[test]
    fn provider_event_ids_are_available_for_idempotency() {
        let event = InboundEvent::CallStatus {
            event_id: "CA123".to_string(),
            status: "completed".to_string(),
        };
        assert_eq!(event.event_id(), Some("CA123"));
        assert_eq!(
            InboundEvent::SessionStarted { template_id: None }.event_id(),
            None
        );
    }
}
