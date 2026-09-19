mod ai_client;
mod session;
mod state_machine;
mod vad;
mod ws_handler;

use std::net::SocketAddr;

use ai_client::AiClient;
use tracing_subscriber::EnvFilter;
use ws_handler::AppState;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")))
        .init();

    let ai_service_url =
        std::env::var("AI_SERVICE_URL").unwrap_or_else(|_| "http://127.0.0.1:8000".to_string());
    let bind_addr = std::env::var("GATEWAY_BIND").unwrap_or_else(|_| "127.0.0.1:8787".to_string());

    let state = AppState {
        ai: AiClient::new(ai_service_url.clone()),
    };

    let app = ws_handler::router(state).route("/health", axum::routing::get(|| async { "ok" }));

    let addr: SocketAddr = bind_addr.parse().expect("invalid GATEWAY_BIND address");
    tracing::info!(%addr, ai_service_url, "gateway listening");

    let listener = tokio::net::TcpListener::bind(addr).await.expect("failed to bind");
    axum::serve(listener, app).await.expect("server error");
}
