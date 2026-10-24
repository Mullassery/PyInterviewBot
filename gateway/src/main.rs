mod ai_client;
mod audit;
mod calendar;
mod native_voice;
mod reminders;
mod scheduler;
mod session;
mod state_machine;
mod twilio;
mod vad;
mod ws_handler;

use std::net::SocketAddr;

use std::sync::Arc;

use ai_client::AiClient;
use session::{SessionConfig, DEFAULT_MAX_UTTERANCE_BYTES};
use tracing_subscriber::EnvFilter;
use vad::DEFAULT_SILENCE_MS_TO_END_TURN;
#[cfg(feature = "local-neural")]
use voice_runtime::NeuralEngine;
use voice_runtime::{
    EngineCatalog, ManagedCallClient, ManagedCallConfig, ManagedCallWorkflow, MockSpeechEngine,
    SpeechStudio, StudioConfig, WebhookVerifier,
};
use ws_handler::AppState;

fn env_usize(name: &str, default: usize) -> usize {
    std::env::var(name)
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(default)
}

fn env_u32(name: &str, default: u32) -> u32 {
    std::env::var(name)
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(default)
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .init();

    let ai_service_url =
        std::env::var("AI_SERVICE_URL").unwrap_or_else(|_| "http://127.0.0.1:8000".to_string());
    let bind_addr = std::env::var("GATEWAY_BIND").unwrap_or_else(|_| "127.0.0.1:8787".to_string());

    let ai_service_api_key = std::env::var("AI_SERVICE_API_KEY")
        .or_else(|_| std::env::var("PYINTERVIEWBOT_API_KEY"))
        .ok()
        .filter(|key| !key.is_empty());
    let template_id = std::env::var("PYINTERVIEWBOT_TEMPLATE")
        .ok()
        .filter(|value| !value.is_empty());
    let session_config = SessionConfig {
        silence_ms: env_u32("GATEWAY_VAD_SILENCE_MS", DEFAULT_SILENCE_MS_TO_END_TURN),
        max_utterance_bytes: env_usize("GATEWAY_MAX_UTTERANCE_BYTES", DEFAULT_MAX_UTTERANCE_BYTES),
    };

    let requested_engine = std::env::var("VOICE_ENGINE").unwrap_or_else(|_| "mock".to_string());
    let mut engines = EngineCatalog::new(requested_engine.clone())
        .register(Arc::new(MockSpeechEngine::default()));
    #[cfg(feature = "local-neural")]
    {
        match NeuralEngine::from_env() {
            Ok(Some(engine)) => {
                tracing::info!(requested = %requested_engine, "registered neural speech engine");
                engines = engines.register(Arc::new(engine));
            }
            Ok(None) => {}
            Err(error) => tracing::warn!(%error, "neural speech engine not registered"),
        }
    }
    if engines.engine(&requested_engine).is_none() {
        tracing::warn!(
            requested = %requested_engine,
            "requested speech engine is unavailable; falling back to mock"
        );
        engines = engines.with_default("mock");
    }
    let engines = Arc::new(engines);
    let studio = Arc::new(SpeechStudio::new(
        engines
            .default_engine()
            .expect("fallback mock engine is registered"),
        StudioConfig::default(),
    ));
    let twilio = twilio::TwilioConfig::from_env().map(Arc::new);
    let scheduler = scheduler::init_from_env();
    let calendar = calendar::CalendarClient::from_env().map(Arc::new);
    let audit = audit::from_env();
    let managed_config = ManagedCallConfig::from_env();
    let managed_call = ManagedCallClient::new(managed_config.clone())
        .ok()
        .map(|client| {
            let max_webhook_bytes = managed_config.max_webhook_bytes;
            Arc::new(ManagedCallWorkflow::new(
                client,
                WebhookVerifier::new(&managed_config),
                max_webhook_bytes,
            ))
        });

    let state = AppState {
        ai: AiClient::new(ai_service_url.clone())
            .with_api_key(ai_service_api_key)
            .with_template(template_id),
        api_key: std::env::var("GATEWAY_API_KEY")
            .ok()
            .filter(|k| !k.is_empty()),
        session_config,
        studio,
        engines,
        managed_call,
        twilio,
        scheduler,
        calendar,
        audit,
    };
    scheduler::spawn_worker(
        state.clone(),
        std::env::var("SCHEDULER_INTERVAL_SECONDS")
            .ok()
            .and_then(|value| value.parse().ok())
            .unwrap_or(5),
    );

    let app = ws_handler::router(state).route("/health", axum::routing::get(|| async { "ok" }));

    let addr: SocketAddr = bind_addr.parse().expect("invalid GATEWAY_BIND address");
    tracing::info!(%addr, ai_service_url, "gateway listening");

    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .expect("failed to bind");
    axum::serve(listener, app).await.expect("server error");
}
