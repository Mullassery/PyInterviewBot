//! Self-hosted, provider-neutral voice runtime for PyInterviewBot.
//!
//! The runtime owns session lifecycle, sequencing, bounded audio, cancellation,
//! idle timeouts, and normalized events. It deliberately does not own interview
//! templates, questions, evidence, or evaluation; those remain in the interview
//! domain layer.

pub mod events;
pub mod managed_call;
pub mod mock;
pub mod provider;
pub mod runtime;
pub mod state;
pub mod studio;

pub use events::{CompletionReason, SessionId, VoiceEvent};
pub use managed_call::{
    normalize_managed_event, ManagedCall, ManagedCallClient, ManagedCallConfig, ManagedCallError,
    ManagedCallWorkflow, NormalizedManagedEvent, WebhookAuth, WebhookVerifier,
};
pub use mock::MockVoiceProvider;
pub use provider::{
    ProviderCapabilities, ProviderError, ProviderEvent, SessionConfig, VoiceProvider,
};
pub use runtime::{RuntimeConfig, RuntimeError, VoiceRuntime};
pub use state::{transition as transition_voice, VoiceState, VoiceTransition};
pub use studio::{
    AudioBuffer, AudioFormat, MockSpeechEngine, SpeechEngine, SpeechStudio, StudioCapabilities,
    StudioConfig, StudioError, StudioEvent, StudioSessionId,
};
