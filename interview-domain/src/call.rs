//! Provider-neutral call lifecycle and capability contracts.
//!
//! A call is a transport concern. It is deliberately separate from the
//! interview session state machine: a provider outage can fail a call without
//! losing interview evidence, and an interview can continue on another channel
//! after a failed call.

use std::fmt;

use serde::{Deserialize, Serialize};
use thiserror::Error;
use uuid::Uuid;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CallProvider {
    Native,
    Twilio,
    Vapi,
    WhatsApp,
    Teams,
}

impl CallProvider {
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "native" => Some(Self::Native),
            "twilio" => Some(Self::Twilio),
            "vapi" => Some(Self::Vapi),
            "whatsapp" => Some(Self::WhatsApp),
            "teams" => Some(Self::Teams),
            _ => None,
        }
    }

    pub fn capabilities(self) -> CallCapabilities {
        match self {
            // In-process voice studio. It has no external transport.
            Self::Native => CallCapabilities {
                realtime_audio: true,
                streaming_transcripts: true,
                final_transcripts: true,
                recording: true,
                ..CallCapabilities::default()
            },
            // Implemented Twilio adapter: telephony and WhatsApp text/voice
            // notes. Recording and live transcript streaming are not wired.
            Self::Twilio => CallCapabilities {
                outbound_telephony: true,
                inbound_telephony: true,
                realtime_audio: true,
                messaging: true,
                voice_notes: true,
                final_transcripts: true,
                ..CallCapabilities::default()
            },
            // Implemented Vapi adapter: outbound/inbound calls and webhook
            // transcripts. Recording artifacts and transfers are not persisted.
            Self::Vapi => CallCapabilities {
                outbound_telephony: true,
                inbound_telephony: true,
                realtime_audio: true,
                streaming_transcripts: true,
                final_transcripts: true,
                tool_calls: true,
                ..CallCapabilities::default()
            },
            // Async WhatsApp adapter. No live call or realtime audio.
            Self::WhatsApp => CallCapabilities {
                messaging: true,
                voice_notes: true,
                final_transcripts: true,
                ..CallCapabilities::default()
            },
            // Teams scheduling exists; joining and transcript ingestion do not.
            Self::Teams => CallCapabilities::default(),
        }
    }
}

impl fmt::Display for CallProvider {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let value = match self {
            Self::Native => "native",
            Self::Twilio => "twilio",
            Self::Vapi => "vapi",
            Self::WhatsApp => "whatsapp",
            Self::Teams => "teams",
        };
        f.write_str(value)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CallDirection {
    Inbound,
    Outbound,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CallTransport {
    Browser,
    Pstn,
    WhatsAppText,
    WhatsAppVoice,
    TeamsMeeting,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct CallCapabilities {
    pub outbound_telephony: bool,
    pub inbound_telephony: bool,
    pub realtime_audio: bool,
    pub messaging: bool,
    pub voice_notes: bool,
    pub streaming_transcripts: bool,
    pub final_transcripts: bool,
    pub recording: bool,
    pub transfer: bool,
    pub meeting_join: bool,
    pub meeting_transcripts: bool,
    pub tool_calls: bool,
}

impl CallCapabilities {
    pub fn supports(self, capability: CallCapability) -> bool {
        match capability {
            CallCapability::OutboundTelephony => self.outbound_telephony,
            CallCapability::InboundTelephony => self.inbound_telephony,
            CallCapability::RealtimeAudio => self.realtime_audio,
            CallCapability::Messaging => self.messaging,
            CallCapability::VoiceNotes => self.voice_notes,
            CallCapability::StreamingTranscripts => self.streaming_transcripts,
            CallCapability::FinalTranscripts => self.final_transcripts,
            CallCapability::Recording => self.recording,
            CallCapability::Transfer => self.transfer,
            CallCapability::MeetingJoin => self.meeting_join,
            CallCapability::MeetingTranscripts => self.meeting_transcripts,
            CallCapability::ToolCalls => self.tool_calls,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CallCapability {
    OutboundTelephony,
    InboundTelephony,
    RealtimeAudio,
    Messaging,
    VoiceNotes,
    StreamingTranscripts,
    FinalTranscripts,
    Recording,
    Transfer,
    MeetingJoin,
    MeetingTranscripts,
    ToolCalls,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct CallRequirements {
    pub direction: CallDirection,
    pub transport: CallTransport,
    pub streaming_transcripts: bool,
    pub final_transcripts: bool,
    pub recording: bool,
    pub transfer: bool,
}

impl Default for CallRequirements {
    fn default() -> Self {
        Self {
            direction: CallDirection::Outbound,
            transport: CallTransport::Pstn,
            streaming_transcripts: false,
            final_transcripts: false,
            recording: false,
            transfer: false,
        }
    }
}

impl CallRequirements {
    pub fn required_capabilities(&self) -> Vec<CallCapability> {
        let mut required = Vec::new();
        match self.transport {
            CallTransport::Browser => required.push(CallCapability::RealtimeAudio),
            CallTransport::Pstn => {
                required.push(match self.direction {
                    CallDirection::Inbound => CallCapability::InboundTelephony,
                    CallDirection::Outbound => CallCapability::OutboundTelephony,
                });
                required.push(CallCapability::RealtimeAudio);
            }
            CallTransport::WhatsAppText => required.push(CallCapability::Messaging),
            CallTransport::WhatsAppVoice => required.push(CallCapability::VoiceNotes),
            CallTransport::TeamsMeeting => required.push(CallCapability::MeetingJoin),
        }
        if self.streaming_transcripts {
            required.push(CallCapability::StreamingTranscripts);
        }
        if self.final_transcripts {
            required.push(CallCapability::FinalTranscripts);
        }
        if self.recording {
            required.push(CallCapability::Recording);
        }
        if self.transfer {
            required.push(CallCapability::Transfer);
        }
        required
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ProviderRegistration {
    pub provider: CallProvider,
    pub configured: bool,
}

impl ProviderRegistration {
    pub fn new(provider: CallProvider, configured: bool) -> Self {
        Self {
            provider,
            configured,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum CallResolutionError {
    #[error("no call provider is registered")]
    NoProvider,
    #[error("provider {provider} cannot satisfy: {missing:?}")]
    Unsupported {
        provider: CallProvider,
        missing: Vec<CallCapability>,
    },
}

#[derive(Debug, Clone, Default)]
pub struct ProviderRegistry {
    providers: Vec<ProviderRegistration>,
}

impl ProviderRegistry {
    pub fn new(providers: Vec<ProviderRegistration>) -> Self {
        Self { providers }
    }

    pub fn providers(&self) -> &[ProviderRegistration] {
        &self.providers
    }

    /// Resolves in registration order. A configured provider that cannot meet
    /// the requirements is reported rather than silently downgraded.
    pub fn resolve(
        &self,
        requirements: &CallRequirements,
    ) -> Result<CallProvider, CallResolutionError> {
        let required = requirements.required_capabilities();
        let mut first_missing: Option<(CallProvider, Vec<CallCapability>)> = None;
        for registration in &self.providers {
            if !registration.configured {
                continue;
            }
            let capabilities = registration.provider.capabilities();
            let missing: Vec<_> = required
                .iter()
                .copied()
                .filter(|capability| !capabilities.supports(*capability))
                .collect();
            if missing.is_empty() {
                return Ok(registration.provider);
            }
            if first_missing.is_none() {
                first_missing = Some((registration.provider, missing));
            }
        }
        match first_missing {
            Some((provider, missing)) => {
                Err(CallResolutionError::Unsupported { provider, missing })
            }
            None => Err(CallResolutionError::NoProvider),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CallState {
    Requested,
    Validating,
    Initiating,
    Ringing,
    Connecting,
    Connected,
    Active,
    Completing,
    Completed,
    Cancelled,
    Declined,
    Busy,
    NoAnswer,
    Failed,
    Disconnected,
    TimedOut,
}

impl CallState {
    pub fn is_terminal(self) -> bool {
        matches!(
            self,
            Self::Completed
                | Self::Cancelled
                | Self::Declined
                | Self::Busy
                | Self::NoAnswer
                | Self::Failed
                | Self::Disconnected
                | Self::TimedOut
        )
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CallTransition {
    Validated,
    Initiate,
    Ringing,
    Connect,
    Activate,
    BeginCompletion,
    Complete,
    Cancel,
    Decline,
    Busy,
    NoAnswer,
    Fail,
    Disconnect,
    Timeout,
}

#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum CallError {
    #[error("call is terminal in state {state:?}")]
    TerminalState { state: CallState },
    #[error("stale call event: last sequence {last}, received {received}")]
    StaleEvent { last: u64, received: u64 },
    #[error("illegal call transition {transition:?} from {state:?}")]
    IllegalTransition {
        state: CallState,
        transition: CallTransition,
    },
    #[error("provider call id is already linked to a different call")]
    ProviderCallConflict,
    #[error("provider call id cannot be empty")]
    EmptyProviderCallId,
}

pub fn transition(state: CallState, event: CallTransition) -> Result<CallState, CallError> {
    use CallState as S;
    use CallTransition as T;
    let next = match (state, event) {
        (S::Requested, T::Validated) => S::Validating,
        (S::Requested, T::Cancel) => S::Cancelled,
        (S::Requested, T::Fail) => S::Failed,

        (S::Validating, T::Initiate) => S::Initiating,
        (S::Validating, T::Cancel) => S::Cancelled,
        (S::Validating, T::Fail) => S::Failed,

        (S::Initiating, T::Ringing) => S::Ringing,
        (S::Initiating, T::Connect) => S::Connected,
        (S::Initiating, T::Cancel) => S::Cancelled,
        (S::Initiating, T::Decline) => S::Declined,
        (S::Initiating, T::Busy) => S::Busy,
        (S::Initiating, T::NoAnswer) => S::NoAnswer,
        (S::Initiating, T::Timeout) => S::TimedOut,
        (S::Initiating, T::Fail) => S::Failed,

        (S::Ringing, T::Connect) => S::Connected,
        (S::Ringing, T::Cancel) => S::Cancelled,
        (S::Ringing, T::Decline) => S::Declined,
        (S::Ringing, T::Busy) => S::Busy,
        (S::Ringing, T::NoAnswer) => S::NoAnswer,
        (S::Ringing, T::Timeout) => S::TimedOut,
        (S::Ringing, T::Fail) => S::Failed,

        (S::Connecting, T::Connect) => S::Connected,
        (S::Connecting, T::Cancel) => S::Cancelled,
        (S::Connecting, T::Timeout) => S::TimedOut,
        (S::Connecting, T::Fail) => S::Failed,

        (S::Connected, T::Activate) => S::Active,
        (S::Connected, T::BeginCompletion) => S::Completing,
        (S::Connected, T::Cancel) => S::Cancelled,
        (S::Connected, T::Disconnect) => S::Disconnected,
        (S::Connected, T::Timeout) => S::TimedOut,
        (S::Connected, T::Fail) => S::Failed,

        (S::Active, T::BeginCompletion) => S::Completing,
        (S::Active, T::Cancel) => S::Cancelled,
        (S::Active, T::Disconnect) => S::Disconnected,
        (S::Active, T::Timeout) => S::TimedOut,
        (S::Active, T::Fail) => S::Failed,

        (S::Completing, T::Complete) => S::Completed,
        (S::Completing, T::Disconnect) => S::Disconnected,
        (S::Completing, T::Timeout) => S::TimedOut,
        (S::Completing, T::Fail) => S::Failed,

        (state, event) => {
            return Err(CallError::IllegalTransition {
                state,
                transition: event,
            })
        }
    };
    Ok(next)
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CallSession {
    pub id: Uuid,
    pub interview_session_id: Option<Uuid>,
    pub provider: CallProvider,
    pub provider_call_id: Option<String>,
    pub direction: CallDirection,
    pub transport: CallTransport,
    pub state: CallState,
    pub version: u64,
    pub last_sequence: Option<u64>,
    pub created_at_ms: u64,
    pub updated_at_ms: u64,
    pub ended_at_ms: Option<u64>,
    pub terminal_transition: Option<CallTransition>,
}

impl CallSession {
    pub fn new(
        id: Uuid,
        interview_session_id: Option<Uuid>,
        provider: CallProvider,
        direction: CallDirection,
        transport: CallTransport,
        created_at_ms: u64,
    ) -> Self {
        Self {
            id,
            interview_session_id,
            provider,
            provider_call_id: None,
            direction,
            transport,
            state: CallState::Requested,
            version: 0,
            last_sequence: None,
            created_at_ms,
            updated_at_ms: created_at_ms,
            ended_at_ms: None,
            terminal_transition: None,
        }
    }

    pub fn state(&self) -> CallState {
        self.state
    }

    pub fn version(&self) -> u64 {
        self.version
    }

    pub fn last_sequence(&self) -> Option<u64> {
        self.last_sequence
    }

    pub fn provider_call_id(&self) -> Option<&str> {
        self.provider_call_id.as_deref()
    }

    pub fn link_provider_call(
        &mut self,
        provider_call_id: impl Into<String>,
    ) -> Result<(), CallError> {
        let provider_call_id = provider_call_id.into();
        let provider_call_id = provider_call_id.trim();
        if provider_call_id.is_empty() {
            return Err(CallError::EmptyProviderCallId);
        }
        match self.provider_call_id.as_deref() {
            Some(existing) if existing != provider_call_id => Err(CallError::ProviderCallConflict),
            _ => {
                self.provider_call_id = Some(provider_call_id.to_string());
                Ok(())
            }
        }
    }

    pub fn apply(
        &mut self,
        event: CallTransition,
        sequence: Option<u64>,
        occurred_at_ms: u64,
    ) -> Result<CallState, CallError> {
        if self.state.is_terminal() {
            return Err(CallError::TerminalState { state: self.state });
        }
        if let (Some(last), Some(received)) = (self.last_sequence, sequence) {
            if received <= last {
                return Err(CallError::StaleEvent { last, received });
            }
        }
        let next = transition(self.state, event)?;
        self.state = next;
        self.version = self.version.saturating_add(1);
        self.updated_at_ms = occurred_at_ms;
        if let Some(sequence) = sequence {
            self.last_sequence = Some(sequence);
        }
        if next.is_terminal() {
            self.ended_at_ms = Some(occurred_at_ms);
            self.terminal_transition = Some(event);
        }
        Ok(next)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn requested(provider: CallProvider) -> CallSession {
        CallSession::new(
            Uuid::new_v4(),
            Some(Uuid::new_v4()),
            provider,
            CallDirection::Outbound,
            CallTransport::Pstn,
            1_000,
        )
    }

    #[test]
    fn call_advances_through_the_documented_lifecycle() {
        let mut call = requested(CallProvider::Vapi);
        for (event, sequence) in [
            (CallTransition::Validated, 1),
            (CallTransition::Initiate, 2),
            (CallTransition::Ringing, 3),
            (CallTransition::Connect, 4),
            (CallTransition::Activate, 5),
            (CallTransition::BeginCompletion, 6),
            (CallTransition::Complete, 7),
        ] {
            call.apply(event, Some(sequence), 1_000 + sequence).unwrap();
        }
        assert_eq!(call.state(), CallState::Completed);
        assert_eq!(call.version(), 7);
        assert_eq!(call.last_sequence(), Some(7));
        assert_eq!(call.ended_at_ms, Some(1_007));
    }

    #[test]
    fn terminal_calls_reject_late_events() {
        let mut call = requested(CallProvider::Twilio);
        call.apply(CallTransition::Fail, Some(1), 1_001).unwrap();
        assert_eq!(call.state(), CallState::Failed);
        let error = call
            .apply(CallTransition::Activate, Some(2), 1_002)
            .unwrap_err();
        assert!(matches!(error, CallError::TerminalState { .. }));
        assert_eq!(call.state(), CallState::Failed);
    }

    #[test]
    fn out_of_order_events_do_not_regress_state() {
        let mut call = requested(CallProvider::Twilio);
        call.apply(CallTransition::Validated, Some(1), 1_001)
            .unwrap();
        call.apply(CallTransition::Initiate, Some(2), 1_002)
            .unwrap();
        call.apply(CallTransition::Ringing, Some(3), 1_003).unwrap();
        let error = call
            .apply(CallTransition::Initiate, Some(2), 1_004)
            .unwrap_err();
        assert!(matches!(error, CallError::StaleEvent { .. }));
        assert_eq!(call.state(), CallState::Ringing);
    }

    #[test]
    fn provider_call_id_can_only_be_linked_once() {
        let mut call = requested(CallProvider::Vapi);
        call.link_provider_call("call-1").unwrap();
        assert_eq!(call.provider_call_id(), Some("call-1"));
        call.link_provider_call("call-1").unwrap();
        assert!(call.link_provider_call("call-2").is_err());
        assert!(call.link_provider_call("").is_err());
    }

    #[test]
    fn capabilities_describe_the_adapter_not_the_vendor() {
        assert!(CallProvider::Twilio.capabilities().outbound_telephony);
        assert!(!CallProvider::WhatsApp.capabilities().outbound_telephony);
        assert!(CallProvider::Vapi.capabilities().streaming_transcripts);
        assert!(!CallProvider::Teams.capabilities().meeting_transcripts);
    }

    #[test]
    fn capability_resolution_rejects_whatsapp_for_live_calls() {
        let registry = ProviderRegistry::new(vec![ProviderRegistration::new(
            CallProvider::WhatsApp,
            true,
        )]);
        let requirements = CallRequirements {
            direction: CallDirection::Outbound,
            transport: CallTransport::Pstn,
            ..CallRequirements::default()
        };
        let error = registry.resolve(&requirements).unwrap_err();
        assert!(matches!(
            error,
            CallResolutionError::Unsupported {
                provider: CallProvider::WhatsApp,
                ..
            }
        ));
    }

    #[test]
    fn capability_resolution_skips_unconfigured_providers() {
        let registry = ProviderRegistry::new(vec![
            ProviderRegistration::new(CallProvider::Twilio, false),
            ProviderRegistration::new(CallProvider::Vapi, true),
        ]);
        let requirements = CallRequirements {
            direction: CallDirection::Outbound,
            transport: CallTransport::Pstn,
            streaming_transcripts: true,
            ..CallRequirements::default()
        };
        assert_eq!(registry.resolve(&requirements).unwrap(), CallProvider::Vapi);
    }

    #[test]
    fn capability_resolution_is_deterministic_by_registration_order() {
        let registry = ProviderRegistry::new(vec![
            ProviderRegistration::new(CallProvider::Twilio, true),
            ProviderRegistration::new(CallProvider::Vapi, true),
        ]);
        let requirements = CallRequirements {
            direction: CallDirection::Outbound,
            transport: CallTransport::Pstn,
            ..CallRequirements::default()
        };
        assert_eq!(
            registry.resolve(&requirements).unwrap(),
            CallProvider::Twilio
        );
        let registry = ProviderRegistry::new(vec![
            ProviderRegistration::new(CallProvider::Vapi, true),
            ProviderRegistration::new(CallProvider::Twilio, true),
        ]);
        assert_eq!(registry.resolve(&requirements).unwrap(), CallProvider::Vapi);
    }
}
