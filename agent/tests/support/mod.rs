//! Integration-test harness: the real agent service, wired to two fakes.
//!
//! * [`FakeLlm`] speaks the OpenAI Chat Completions wire (streaming and not),
//!   replaying a script of model turns: text chunks, tool calls, errors. The
//!   agent reaches it through Rig's real OpenAI provider, so these tests cover
//!   the production model path, not a mock of it.
//! * [`FakeMcp`] is an MCP server built with the same `rmcp` macros as
//!   `mcp-server/`, over streamable HTTP, behind the same bearer check. Its
//!   `/approvals/execute` verifies tokens with the MCP server's *own* verifier
//!   source (`mcp-server/src/approval.rs`, compiled in below), so an approval
//!   minted by this agent is accepted here only if the real server would.
//!
//! Every test gets its own instances on ephemeral ports.

#![allow(dead_code)]

use std::{
    collections::{BTreeMap, HashSet, VecDeque},
    net::SocketAddr,
    sync::{Arc, Mutex},
    time::Duration,
};

use axum::{
    Json, Router,
    body::{Body, Bytes},
    extract::{Request, State},
    http::{StatusCode, header},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::post,
};
use futures::StreamExt;
use rmcp::{
    ErrorData as McpError, ServerHandler,
    handler::server::{router::tool::ToolRouter, wrapper::Parameters},
    model::*,
    tool, tool_handler, tool_router,
    transport::streamable_http_server::{
        StreamableHttpServerConfig, StreamableHttpService, session::local::LocalSessionManager,
    },
};
use schemars::JsonSchema;
use serde::Deserialize;
use serde_json::{Value, json};
use tickets_agent::{
    config::{AgentConfigFile, ApprovalToolConfig, ApprovalToolsConfig, Settings},
    services::Services,
};

#[path = "../../../mcp-server/src/approval.rs"]
#[allow(dead_code, clippy::all)]
// Another crate's source: never reformatted from here.
#[rustfmt::skip]
pub mod mcp_approval;

pub const GATEWAY_KEY: &str = "test-gateway-key";
pub const MCP_KEY: &str = "test-mcp-key";
pub const APPROVAL_SECRET: &str = "test-approval-secret-of-at-least-24";

// ---------------------------------------------------------------------------
// Fake LLM
// ---------------------------------------------------------------------------

/// One scripted agent-model turn.
#[derive(Clone, Debug)]
pub enum Turn {
    /// Assistant text, streamed in exactly these chunks.
    Text(Vec<String>),
    /// One or more tool calls: (id, name, raw argument JSON).
    ToolCalls(Vec<(String, String, String)>),
    /// The provider answers with this HTTP status.
    HttpError(u16),
    /// Stream these chunks, then stall forever (for disconnect tests).
    Stall(Vec<String>),
}

pub fn text(chunks: &[&str]) -> Turn {
    Turn::Text(chunks.iter().map(|chunk| (*chunk).to_string()).collect())
}

pub fn call(name: &str, args: Value) -> Turn {
    Turn::ToolCalls(vec![(format!("call_{}", uuid::Uuid::new_v4().simple()), name.into(), args.to_string())])
}

pub fn raw_call(name: &str, raw_args: &str) -> Turn {
    Turn::ToolCalls(vec![(format!("call_{}", uuid::Uuid::new_v4().simple()), name.into(), raw_args.into())])
}

#[derive(Default)]
pub struct LlmState {
    pub script: Mutex<VecDeque<Turn>>,
    /// The guard model's reply to every classification.
    pub verdict: Mutex<String>,
    /// Every request body the fake received, in order.
    pub requests: Mutex<Vec<Value>>,
}

#[derive(Clone)]
pub struct FakeLlm {
    pub url: String,
    pub state: Arc<LlmState>,
}

impl FakeLlm {
    pub fn agent_requests(&self) -> Vec<Value> {
        self.state.requests.lock().unwrap().iter().filter(|r| r["stream"] == true).cloned().collect()
    }

    pub fn guard_requests(&self) -> Vec<Value> {
        self.state.requests.lock().unwrap().iter().filter(|r| r["stream"] != true).cloned().collect()
    }
}

fn chunk(delta: Value, finish: Option<&str>) -> String {
    let body = json!({
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "fake",
        "choices": [{ "index": 0, "delta": delta, "finish_reason": finish }]
    });
    format!("data: {body}\n\n")
}

async fn completions(State(state): State<Arc<LlmState>>, Json(body): Json<Value>) -> Response {
    state.requests.lock().unwrap().push(body.clone());
    if body["stream"] != true {
        let verdict = state.verdict.lock().unwrap().clone();
        return Json(json!({
            "id": "chatcmpl-guard",
            "object": "chat.completion",
            "created": 0,
            "model": "fake",
            "choices": [{ "index": 0, "message": { "role": "assistant", "content": verdict }, "finish_reason": "stop" }],
            "usage": { "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2 }
        }))
        .into_response();
    }
    let turn = state.script.lock().unwrap().pop_front().unwrap_or_else(|| text(&["(script exhausted)"]));
    let frames: Vec<String> = match &turn {
        Turn::HttpError(status) => {
            return (
                StatusCode::from_u16(*status).unwrap(),
                Json(json!({"error": {"message": "provider exploded at http://internal-llm:9999"}})),
            )
                .into_response();
        }
        Turn::Text(chunks) | Turn::Stall(chunks) => {
            let mut frames = vec![chunk(json!({"role": "assistant", "content": ""}), None)];
            frames.extend(chunks.iter().map(|text| chunk(json!({"content": text}), None)));
            if matches!(turn, Turn::Text(_)) {
                frames.push(chunk(json!({}), Some("stop")));
                frames.push("data: [DONE]\n\n".into());
            }
            frames
        }
        Turn::ToolCalls(calls) => {
            let mut frames = vec![chunk(json!({"role": "assistant", "content": null}), None)];
            for (index, (id, name, args)) in calls.iter().enumerate() {
                frames.push(chunk(
                    json!({"tool_calls": [{"index": index, "id": id, "type": "function", "function": {"name": name, "arguments": ""}}]}),
                    None,
                ));
                frames.push(chunk(json!({"tool_calls": [{"index": index, "function": {"arguments": args}}]}), None));
            }
            frames.push(chunk(json!({}), Some("tool_calls")));
            frames.push("data: [DONE]\n\n".into());
            frames
        }
    };
    let stall = matches!(turn, Turn::Stall(_));
    let stream =
        futures::stream::iter(frames.into_iter().map(|frame| Ok::<_, std::convert::Infallible>(Bytes::from(frame))))
            .chain(futures::stream::once(async move {
                if stall {
                    futures::future::pending::<()>().await;
                }
                Ok(Bytes::new())
            }));
    Response::builder().header(header::CONTENT_TYPE, "text/event-stream").body(Body::from_stream(stream)).unwrap()
}

pub async fn fake_llm(script: Vec<Turn>) -> FakeLlm {
    let state = Arc::new(LlmState::default());
    *state.script.lock().unwrap() = script.into();
    *state.verdict.lock().unwrap() = "No".into();
    let app = Router::new().route("/v1/chat/completions", post(completions)).with_state(Arc::clone(&state));
    let url = format!("http://{}/v1", serve(app).await);
    FakeLlm { url, state }
}

// ---------------------------------------------------------------------------
// Fake MCP server
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize, JsonSchema)]
struct SearchTicketsArgs {
    /// Optional ticket status, for example "open" or "resolved".
    status: Option<String>,
    /// Maximum number of tickets to return. Defaults to 50 and is capped at 100.
    limit: Option<i64>,
}

#[derive(Debug, Deserialize, JsonSchema)]
struct GetTicketArgs {
    /// Exact ticket identifier, for example "TKT-1001".
    ticket_id: String,
}

#[derive(Default)]
pub struct McpState {
    /// (tool, arguments) for every MCP call received.
    pub calls: Mutex<Vec<(String, Value)>>,
    /// Ticket id → priority.
    pub priorities: Mutex<BTreeMap<String, String>>,
    pub descriptions: Mutex<BTreeMap<String, String>>,
    pub nonces: Mutex<HashSet<String>>,
    /// Claims of every approval token the verifier accepted.
    pub applied: Mutex<Vec<mcp_approval::ApprovalClaims>>,
    /// Raw approval-execution bodies received.
    pub execute_bodies: Mutex<Vec<Value>>,
}

#[derive(Clone)]
struct TicketsServer {
    state: Arc<McpState>,
    #[allow(dead_code)]
    tool_router: ToolRouter<Self>,
}

#[tool_router]
impl TicketsServer {
    #[tool(description = "Search support tickets using optional status and limit filters.")]
    async fn search_tickets(
        &self,
        Parameters(args): Parameters<SearchTicketsArgs>,
    ) -> Result<CallToolResult, McpError> {
        self.state
            .calls
            .lock()
            .unwrap()
            .push(("search_tickets".into(), json!({"status": args.status, "limit": args.limit})));
        let tickets: Vec<Value> = self
            .state
            .priorities
            .lock()
            .unwrap()
            .iter()
            .map(|(id, priority)| json!({"id": id, "priority": priority, "status": "open", "subject": "Subject", "customer_name": "Renee Castillo"}))
            .collect();
        Ok(CallToolResult::success(vec![ContentBlock::text(
            json!({"count": tickets.len(), "tickets": tickets}).to_string(),
        )]))
    }

    #[tool(description = "Get one support ticket by exact ticket_id, including its history.")]
    async fn get_ticket(&self, Parameters(args): Parameters<GetTicketArgs>) -> Result<CallToolResult, McpError> {
        self.state.calls.lock().unwrap().push(("get_ticket".into(), json!({"ticket_id": args.ticket_id})));
        let Some(priority) = self.state.priorities.lock().unwrap().get(&args.ticket_id).cloned() else {
            return Err(McpError::invalid_params(format!("Ticket '{}' was not found", args.ticket_id), None));
        };
        let description = self
            .state
            .descriptions
            .lock()
            .unwrap()
            .get(&args.ticket_id)
            .cloned()
            .unwrap_or_else(|| "A delayed delivery.".into());
        Ok(CallToolResult::success(vec![ContentBlock::text(
            json!({"ticket": {"id": args.ticket_id, "priority": priority, "description": description}, "history_count": 0, "history": []}).to_string(),
        )]))
    }
}

#[tool_handler]
impl ServerHandler for TicketsServer {
    fn get_info(&self) -> ServerInfo {
        ServerInfo::new(ServerCapabilities::builder().enable_tools().build())
    }
}

#[derive(Deserialize)]
struct ExecuteRequest {
    approval_token: String,
    request_id: String,
}

/// `mutation::execute`, reduced to what the agent can observe: verify with the
/// real verifier against the current priority, consume the nonce, apply.
async fn execute(State(state): State<Arc<McpState>>, Json(raw): Json<Value>) -> Response {
    state.execute_bodies.lock().unwrap().push(raw.clone());
    let Ok(request) = serde_json::from_value::<ExecuteRequest>(raw) else {
        return (StatusCode::BAD_REQUEST, Json(json!({"ok": false, "result": {"error": "bad body"}}))).into_response();
    };
    let verifier = mcp_approval::ApprovalVerifier::new(Arc::new(APPROVAL_SECRET.as_bytes().to_vec()));
    let peek = match verifier.decode(&request.approval_token) {
        Ok(claims) => claims,
        Err(error) => {
            return (StatusCode::FORBIDDEN, Json(json!({"ok": false, "result": {"error": error}}))).into_response();
        }
    };
    let current = state.priorities.lock().unwrap().get(&peek.resource_id).cloned();
    let Some(current) = current else {
        return Json(json!({"ok": false, "result": {"refused": "no such ticket"}})).into_response();
    };
    let claims = match verifier.verify(
        &request.approval_token,
        "set_ticket_priority",
        &peek.resource_id,
        &request.request_id,
        Some(&current),
    ) {
        Ok(claims) => claims,
        Err(error) => {
            return (StatusCode::FORBIDDEN, Json(json!({"ok": false, "result": {"error": error}}))).into_response();
        }
    };
    if !state.nonces.lock().unwrap().insert(claims.nonce.clone()) {
        return Json(json!({"ok": false, "result": {"refused": "this approval has already been used"}}))
            .into_response();
    }
    let choice = claims.choice.clone().unwrap_or_default();
    state.priorities.lock().unwrap().insert(claims.resource_id.clone(), choice.clone());
    state.applied.lock().unwrap().push(claims.clone());
    Json(json!({"ok": true, "result": {"resource_id": claims.resource_id, "previous_priority": current, "new_priority": choice, "actor_id": claims.actor_id}}))
        .into_response()
}

async fn require_key(request: Request, next: Next) -> Response {
    let ok = request.headers().get(header::AUTHORIZATION).and_then(|v| v.to_str().ok())
        == Some(&format!("Bearer {MCP_KEY}"));
    if !ok {
        return (StatusCode::UNAUTHORIZED, "invalid or missing internal API key").into_response();
    }
    next.run(request).await
}

#[derive(Clone)]
pub struct FakeMcp {
    pub url: String,
    pub state: Arc<McpState>,
}

impl FakeMcp {
    pub fn calls(&self) -> Vec<(String, Value)> {
        self.state.calls.lock().unwrap().clone()
    }

    pub fn priority(&self, id: &str) -> Option<String> {
        self.state.priorities.lock().unwrap().get(id).cloned()
    }
}

pub async fn fake_mcp() -> FakeMcp {
    let state = Arc::new(McpState::default());
    {
        let mut priorities = state.priorities.lock().unwrap();
        priorities.insert("TKT-1001".into(), "medium".into());
        priorities.insert("TKT-1002".into(), "urgent".into());
    }
    let tools_state = Arc::clone(&state);
    let service = StreamableHttpService::new(
        move || Ok(TicketsServer { state: Arc::clone(&tools_state), tool_router: TicketsServer::tool_router() }),
        LocalSessionManager::default().into(),
        StreamableHttpServerConfig::default().with_allowed_hosts(vec!["127.0.0.1".to_string()]),
    );
    let app = Router::new()
        .nest_service("/mcp", service)
        .merge(Router::new().route("/approvals/execute", post(execute)).with_state(Arc::clone(&state)))
        .route_layer(middleware::from_fn(require_key));
    let address = serve(app).await;
    FakeMcp { url: format!("http://{address}/mcp"), state }
}

// ---------------------------------------------------------------------------
// The agent under test
// ---------------------------------------------------------------------------

pub struct Agent {
    pub base: String,
    pub llm: FakeLlm,
    pub mcp: FakeMcp,
    pub services: Arc<Services>,
}

#[derive(Clone, Default)]
pub struct Options {
    pub approvals: bool,
    pub env: Vec<(&'static str, String)>,
}

pub async fn agent(script: Vec<Turn>, options: Options) -> Agent {
    let llm = fake_llm(script).await;
    let mcp = fake_mcp().await;
    let mut file = AgentConfigFile::parse(include_str!("../../config.yml")).expect("config.yml parses");
    let mut env: BTreeMap<String, String> = [
        ("NAT_GATEWAY_API_KEY", GATEWAY_KEY.to_string()),
        ("MCP_API_KEY", MCP_KEY.to_string()),
        ("TICKETS_MCP_URL", mcp.url.clone()),
        ("LLM_BASE_URL", llm.url.clone()),
        ("LLM_REASONING_EFFORT", "none".to_string()),
        ("AGENT_BIND_ADDRESS", "127.0.0.1:0".to_string()),
    ]
    .into_iter()
    .map(|(k, v)| (k.to_string(), v))
    .collect();
    if options.approvals {
        file.tools.approval =
            Some(ApprovalToolsConfig { ticket_priority_change: ApprovalToolConfig { token_ttl_seconds: 600 } });
        env.insert("HITL_APPROVAL_SECRET".into(), APPROVAL_SECRET.into());
        env.insert("HITL_ENABLE_INTERACTIVE".into(), "true".into());
        env.insert("HITL_INTERACTION_TIMEOUT_SECONDS".into(), "30".into());
    }
    for (key, value) in options.env {
        env.insert(key.to_string(), value);
    }
    let settings = Settings::resolve(&env, "config.yml".into(), file).expect("settings resolve");
    let services = Services::start(settings).await.expect("services start");
    let address = serve(tickets_agent::api::router(Arc::clone(&services))).await;
    Agent { base: format!("http://{address}"), llm, mcp, services }
}

async fn serve(app: Router) -> SocketAddr {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    address
}

// ---------------------------------------------------------------------------
// Client side: requests and SSE parsing
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
pub enum Event {
    Step { step_type: String, name: String, payload: Value },
    Answer(String),
    Interaction(Value),
    Error(Value),
}

#[derive(Debug, Default)]
pub struct Run {
    pub status: u16,
    pub request_id: Option<String>,
    pub events: Vec<Event>,
}

impl Run {
    pub fn answer(&self) -> String {
        self.events.iter().filter_map(|e| if let Event::Answer(t) = e { Some(t.as_str()) } else { None }).collect()
    }

    pub fn steps(&self, step_type: &str) -> Vec<(String, Value)> {
        self.events
            .iter()
            .filter_map(|e| match e {
                Event::Step { step_type: t, name, payload } if t == step_type => Some((name.clone(), payload.clone())),
                _ => None,
            })
            .collect()
    }

    pub fn decision(&self, name: &str) -> Option<Value> {
        self.steps("FUNCTION_END").into_iter().find(|(n, _)| n == name).map(|(_, p)| p["data"]["output"].clone())
    }

    pub fn errors(&self) -> Vec<Value> {
        self.events.iter().filter_map(|e| if let Event::Error(v) = e { Some(v.clone()) } else { None }).collect()
    }

    pub fn raw(&self) -> String {
        format!("{:?}", self.events)
    }
}

pub fn parse_block(block: &str) -> Option<Event> {
    if let Some(rest) = block.strip_prefix("intermediate_data: ") {
        let value: Value = serde_json::from_str(rest).ok()?;
        return Some(Event::Step {
            step_type: value["type"].as_str()?.to_string(),
            name: value["name"].as_str()?.to_string(),
            payload: value["payload"].clone(),
        });
    }
    if let Some(rest) = block.strip_prefix("data: ") {
        let value: Value = serde_json::from_str(rest).ok()?;
        return Some(Event::Answer(value["value"].as_str()?.to_string()));
    }
    if block.starts_with("event: interaction_required") {
        let data = block.split_once("data: ")?.1;
        return Some(Event::Interaction(serde_json::from_str(data).ok()?));
    }
    if block.starts_with('{') {
        return Some(Event::Error(serde_json::from_str(block).ok()?));
    }
    None
}

pub fn client() -> reqwest::Client {
    reqwest::Client::builder().timeout(Duration::from_secs(60)).build().unwrap()
}

pub fn workflow_request(agent: &Agent, user: &str, messages: Value) -> reqwest::RequestBuilder {
    workflow_request_with_id(agent, user, messages, &uuid::Uuid::new_v4().to_string())
}

/// As the gateway sends it: one gateway-minted `x-request-id`.
pub fn workflow_request_with_id(
    agent: &Agent,
    user: &str,
    messages: Value,
    request_id: &str,
) -> reqwest::RequestBuilder {
    client()
        .post(format!("{}/v1/workflow/full?filter_steps=WORKFLOW_START,WORKFLOW_END,TOOL_START,TOOL_END,FUNCTION_START,FUNCTION_END", agent.base))
        .bearer_auth(GATEWAY_KEY)
        .header("x-authenticated-user-id", user)
        .header("x-request-id", request_id)
        .json(&json!({ "messages": messages }))
}

/// Run one request to completion, collecting every event.
pub async fn ask(agent: &Agent, question: &str) -> Run {
    collect(workflow_request(agent, "support-rep-1", json!([{"role": "user", "content": question}]))).await
}

pub async fn collect(request: reqwest::RequestBuilder) -> Run {
    let response = request.send().await.expect("request sends");
    let status = response.status().as_u16();
    let request_id = response.headers().get("x-request-id").and_then(|v| v.to_str().ok()).map(str::to_string);
    let body = response.text().await.unwrap_or_default();
    let events = body.split("\n\n").filter(|b| !b.trim().is_empty()).filter_map(parse_block).collect();
    Run { status, request_id, events }
}

/// A streaming run whose events can be read one at a time while it is live.
pub struct Live {
    pub events: tokio::sync::mpsc::UnboundedReceiver<Event>,
    pub task: tokio::task::JoinHandle<()>,
}

pub async fn start(request: reqwest::RequestBuilder) -> Live {
    let response = request.send().await.expect("request sends");
    let (sender, events) = tokio::sync::mpsc::unbounded_channel();
    let task = tokio::spawn(async move {
        let mut stream = response.bytes_stream();
        let mut buffer = String::new();
        while let Some(Ok(bytes)) = stream.next().await {
            buffer.push_str(&String::from_utf8_lossy(&bytes));
            while let Some(index) = buffer.find("\n\n") {
                let block: String = buffer.drain(..index + 2).collect();
                if let Some(event) = parse_block(block.trim_end()) {
                    let _ = sender.send(event);
                }
            }
        }
    });
    Live { events, task }
}

impl Live {
    /// The next interaction prompt, skipping everything else.
    pub async fn next_interaction(&mut self) -> Value {
        loop {
            match tokio::time::timeout(Duration::from_secs(30), self.events.recv()).await {
                Ok(Some(Event::Interaction(value))) => return value,
                Ok(Some(_)) => continue,
                other => panic!("no interaction arrived: {other:?}"),
            }
        }
    }

    /// Drain the rest of the stream.
    pub async fn finish(mut self) -> Vec<Event> {
        let mut events = Vec::new();
        while let Ok(Some(event)) = tokio::time::timeout(Duration::from_secs(30), self.events.recv()).await {
            events.push(event);
        }
        events
    }
}

pub async fn respond(agent: &Agent, user: &str, interaction: &Value, response: Value) -> (u16, String) {
    let url = format!(
        "{}/executions/{}/interactions/{}/response",
        agent.base,
        interaction["execution_id"].as_str().unwrap(),
        interaction["interaction_id"].as_str().unwrap()
    );
    let response = client()
        .post(url)
        .bearer_auth(GATEWAY_KEY)
        .header("x-authenticated-user-id", user)
        .json(&json!({ "response": response }))
        .send()
        .await
        .unwrap();
    (response.status().as_u16(), response.text().await.unwrap_or_default())
}

pub fn radio(option: &Value) -> Value {
    json!({"type": "radio", "selected_option": {
        "id": option["id"], "label": option["label"], "value": option["value"], "description": option["description"]
    }})
}

pub fn option(interaction: &Value, id: &str) -> Value {
    interaction["prompt"]["options"].as_array().unwrap().iter().find(|o| o["id"] == id).cloned().unwrap()
}
