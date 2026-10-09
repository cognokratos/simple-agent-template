//! `tickets-agent` — serve the agent, or probe something from inside the
//! container (the runtime image has no shell, curl or Python).

use std::{process::ExitCode, time::Duration};

use tickets_agent::{api, config, services::Services, telemetry};

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let runtime = match tokio::runtime::Builder::new_multi_thread().enable_all().build() {
        Ok(runtime) => runtime,
        Err(error) => {
            eprintln!("could not start the async runtime: {error}");
            return ExitCode::FAILURE;
        }
    };
    match args.first().map(String::as_str) {
        None | Some("serve") => runtime.block_on(serve()),
        Some("probe") => runtime.block_on(probe(&args[1..])),
        Some(other) => {
            eprintln!("unknown command {other:?}; expected `serve` or `probe`");
            ExitCode::FAILURE
        }
    }
}

async fn serve() -> ExitCode {
    let settings = match config::Settings::load(&config::ProcessEnv) {
        Ok(settings) => settings,
        Err(error) => {
            // Before telemetry exists: plain stderr, and a non-zero exit.
            eprintln!("tickets-agent refused to start: {error}");
            return ExitCode::FAILURE;
        }
    };
    let _telemetry = telemetry::init(&settings.telemetry);
    tracing::info!(
        runtime = "rig-rust",
        model = %settings.agent.model,
        guard_model = %settings.guard.model,
        approvals = settings.approval.is_some(),
        "starting tickets-agent"
    );
    let bind = settings.bind_address;
    let services = match Services::start(settings).await {
        Ok(services) => services,
        Err(error) => {
            tracing::error!(%error, "tickets-agent refused to start");
            return ExitCode::FAILURE;
        }
    };
    let listener = match tokio::net::TcpListener::bind(bind).await {
        Ok(listener) => listener,
        Err(error) => {
            tracing::error!(%error, %bind, "could not bind");
            return ExitCode::FAILURE;
        }
    };
    tracing::info!(%bind, "tickets-agent listening");
    let shutdown = async {
        let _ = tokio::signal::ctrl_c().await;
    };
    match axum::serve(listener, api::router(services)).with_graceful_shutdown(shutdown).await {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            tracing::error!(%error, "server failed");
            ExitCode::FAILURE
        }
    }
}

/// Container-side probes, replacing the `python -c …` one-liners `main`'s
/// Makefile runs inside the NAT image.
///
/// * `probe http <url>` — GET, print the status, succeed on 2xx;
/// * `probe status <method> <url>` — print the status of an unauthenticated
///   request, always succeed (the caller asserts on the code);
/// * `probe tcp <host:port>` — succeed if a TCP connection opens;
/// * `probe mcp-auth` — the MCP key boundary (`make verify-mcp`);
/// * `probe version` — this agent's own authenticated `/version`.
async fn probe(args: &[String]) -> ExitCode {
    let client = match reqwest::Client::builder().timeout(Duration::from_secs(5)).build() {
        Ok(client) => client,
        Err(_) => return ExitCode::FAILURE,
    };
    match args.iter().map(String::as_str).collect::<Vec<_>>().as_slice() {
        ["http", url] => match client.get(*url).send().await {
            Ok(response) => {
                println!("{}", response.status().as_u16());
                if response.status().is_success() { ExitCode::SUCCESS } else { ExitCode::FAILURE }
            }
            Err(_) => {
                println!("000");
                ExitCode::FAILURE
            }
        },
        ["status", method, url] => {
            let method = reqwest::Method::from_bytes(method.as_bytes()).unwrap_or(reqwest::Method::GET);
            let code = client.request(method, *url).send().await.map_or(0, |r| r.status().as_u16());
            println!("{code:03}");
            ExitCode::SUCCESS
        }
        ["tcp", address] => {
            match tokio::time::timeout(Duration::from_secs(5), tokio::net::TcpStream::connect(*address)).await {
                Ok(Ok(_)) => ExitCode::SUCCESS,
                _ => ExitCode::FAILURE,
            }
        }
        ["mcp-auth"] => {
            // The MCP boundary, probed from the one service allowed to reach it:
            // no key is refused, the configured key is accepted.
            let url = std::env::var("TICKETS_MCP_URL").unwrap_or_else(|_| "http://mcp-server:8080/mcp".into());
            let key = std::env::var("MCP_API_KEY").unwrap_or_default();
            let anonymous = client.post(&url).send().await.map_or(0, |r| r.status().as_u16());
            if anonymous != 401 {
                eprintln!("MCP without API key returned {anonymous:03}, expected 401");
                return ExitCode::FAILURE;
            }
            if key.trim().is_empty() {
                eprintln!("MCP_API_KEY is missing in the agent container");
                return ExitCode::FAILURE;
            }
            let keyed = client.post(&url).bearer_auth(key.trim()).send().await.map_or(0, |r| r.status().as_u16());
            if keyed == 401 || keyed == 0 {
                eprintln!("MCP rejected the configured API key (status {keyed:03})");
                return ExitCode::FAILURE;
            }
            println!("MCP API-key boundary passed (authenticated endpoint status: {keyed})");
            ExitCode::SUCCESS
        }
        ["version"] => {
            let key = std::env::var("NAT_GATEWAY_API_KEY").unwrap_or_default();
            let response = client
                .get("http://127.0.0.1:8000/version")
                .bearer_auth(key)
                .header("x-authenticated-user-id", "make-version")
                .send()
                .await;
            match response {
                Ok(response) if response.status().is_success() => {
                    println!("{}", response.text().await.unwrap_or_default());
                    ExitCode::SUCCESS
                }
                _ => ExitCode::FAILURE,
            }
        }
        _ => {
            eprintln!("usage: probe http <url> | status <method> <url> | tcp <host:port> | mcp-auth | version");
            ExitCode::FAILURE
        }
    }
}
