//! The MCP client: one session to the Rust MCP server, shared by all requests.
//!
//! MCP stays the capability boundary on this branch. The agent never talks to
//! PostgreSQL; it discovers tools from the existing MCP server, checks their
//! published schemas, and invokes them over MCP with the service credential
//! (`Authorization: Bearer $MCP_API_KEY`), exactly as NAT's `mcp_client`
//! function group does on `main`.
//!
//! Transport and result conversion use Rig's MCP integration (`rig-rmcp`'s
//! [`McpTool`], over `rmcp` 2.2 — the same SDK line as the server). Which tools
//! exist, what their arguments may be, and whether a call is allowed at all
//! are decided here and in `guardrails::tools`, not by Rig.

use std::{collections::BTreeMap, sync::Arc, time::Duration};

use rig_core::tool::ToolOutput;
use rig_rmcp::{McpTool, mcp_result_output};
use rmcp::{
    RoleClient, ServiceExt,
    model::Tool as McpToolDefinition,
    service::RunningService,
    transport::{StreamableHttpClientTransport, streamable_http_client::StreamableHttpClientTransportConfig},
};
use serde_json::{Map, Value};
use tokio::sync::RwLock;

use super::schema::{ObjectSchema, SchemaError};
use crate::config::McpToolsConfig;

#[derive(Debug, thiserror::Error)]
pub enum McpError {
    #[error("could not connect to the MCP server: {0}")]
    Connect(String),
    #[error("could not list MCP tools: {0}")]
    List(String),
    #[error("tool {0:?} is in tools.mcp.include but the MCP server does not offer it")]
    MissingTool(String),
    #[error("tool {tool:?} has an unsupported input schema: {source}")]
    Schema {
        tool: String,
        #[source]
        source: SchemaError,
    },
    #[error("tool {0:?} is annotated as state-changing; MCP tools exposed to the model must be read-only")]
    NotReadOnly(String),
}

/// One tool as the model will see it, plus everything needed to check a call.
#[derive(Debug, Clone)]
pub struct DiscoveredTool {
    pub name: String,
    pub description: String,
    /// The schema exactly as the server published it, advertised to the model.
    pub parameters: Value,
    /// The same schema, parsed into the strict subset used for validation.
    pub schema: ObjectSchema,
    definition: McpToolDefinition,
}

/// Why one MCP call failed. Rendered to the model as a tool error it can
/// correct, as `pass_tool_call_errors_to_agent: true` does on `main`.
#[derive(Debug, thiserror::Error)]
pub enum CallError {
    #[error("{0}")]
    Tool(String),
}

type Session = RunningService<RoleClient, ()>;

pub struct McpCatalog {
    url: String,
    api_key: String,
    timeout: Duration,
    session: RwLock<Session>,
    tools: BTreeMap<String, DiscoveredTool>,
}

async fn connect(url: &str, api_key: &str) -> Result<Session, McpError> {
    let config = StreamableHttpClientTransportConfig::with_uri(url.to_string())
        .auth_header(api_key.to_string())
        // A restarted MCP server forgets its sessions; recover once,
        // transparently, instead of failing every later call.
        .reinit_on_expired_session(true);
    let transport = StreamableHttpClientTransport::with_client(reqwest::Client::new(), config);
    ().serve(transport).await.map_err(|error| McpError::Connect(error.to_string()))
}

impl McpCatalog {
    /// Connect, list tools, and keep only the allow-listed ones whose schemas
    /// this agent can enforce. Anything unexpected stops startup.
    pub async fn discover(url: &str, api_key: &str, config: &McpToolsConfig) -> Result<Self, McpError> {
        let session = connect(url, api_key).await?;
        let offered = session.list_all_tools().await.map_err(|error| McpError::List(error.to_string()))?;
        let tools = select_tools(offered, config)?;
        tracing::info!(tools = ?tools.keys().collect::<Vec<_>>(), "discovered MCP tools");
        Ok(Self {
            url: url.to_string(),
            api_key: api_key.to_string(),
            timeout: Duration::from_secs(config.tool_call_timeout_seconds),
            session: RwLock::new(session),
            tools,
        })
    }

    pub fn tools(&self) -> impl Iterator<Item = &DiscoveredTool> {
        self.tools.values()
    }

    /// Whether the session is still open, for readiness.
    pub async fn is_connected(&self) -> bool {
        !self.session.read().await.is_transport_closed()
    }

    /// Invoke one tool with arguments the tool policy has already validated.
    /// Read-only tools are safe to retry once on a dropped session.
    pub async fn call(&self, name: &str, args: &Map<String, Value>) -> Result<ToolOutput, CallError> {
        let tool = self.tools.get(name).ok_or_else(|| CallError::Tool(format!("tool {name:?} is not available")))?;
        let arguments = Value::Object(args.clone()).to_string();

        let first = self.call_once(tool, &arguments).await;
        let result = match first {
            Err(CallOnceError::Transport(reason)) => {
                tracing::warn!(tool = name, %reason, "MCP session lost; reconnecting once");
                self.reconnect().await.map_err(|error| CallError::Tool(error.to_string()))?;
                self.call_once(tool, &arguments).await
            }
            other => other,
        };
        match result {
            Ok(output) => Ok(output),
            Err(CallOnceError::Transport(reason) | CallOnceError::Tool(reason)) => Err(CallError::Tool(reason)),
        }
    }

    async fn call_once(&self, tool: &DiscoveredTool, arguments: &str) -> Result<ToolOutput, CallOnceError> {
        let peer = self.session.read().await.peer().clone();
        let mcp_tool = McpTool::from_mcp_server(tool.definition.clone(), peer).with_timeout(self.timeout);
        match mcp_tool.execute_mcp(arguments.to_string(), None).await {
            Ok(result) => {
                let output =
                    mcp_result_output(&result).map_err(|error| CallOnceError::Tool(bounded(&error.to_string())))?;
                if result.is_error == Some(true) {
                    // A tool-level error (as opposed to a protocol error): the
                    // content explains it; the model sees it and may correct.
                    return Err(CallOnceError::Tool(bounded(&output_text(&output))));
                }
                Ok(output)
            }
            Err(error) => {
                let message = error.to_string();
                if self.session.read().await.is_transport_closed() || message.contains("Transport closed") {
                    Err(CallOnceError::Transport(bounded(&message)))
                } else {
                    Err(CallOnceError::Tool(bounded(&message)))
                }
            }
        }
    }

    async fn reconnect(&self) -> Result<(), McpError> {
        let fresh = connect(&self.url, &self.api_key).await?;
        *self.session.write().await = fresh;
        Ok(())
    }
}

enum CallOnceError {
    Transport(String),
    Tool(String),
}

/// Text of a tool output, for an error message or a step event.
pub fn output_text(output: &ToolOutput) -> String {
    output.render()
}

/// Bound an upstream message before it reaches the model or a span.
fn bounded(message: &str) -> String {
    const MAX: usize = 600;
    if message.chars().count() <= MAX {
        message.to_string()
    } else {
        let cut: String = message.chars().take(MAX).collect();
        format!("{cut}…")
    }
}

/// Apply the allow-list, the description overrides and the schema check.
/// Pure over the server's tool list, so it is unit-testable without a server.
pub fn select_tools(
    offered: Vec<McpToolDefinition>,
    config: &McpToolsConfig,
) -> Result<BTreeMap<String, DiscoveredTool>, McpError> {
    let mut by_name: BTreeMap<String, McpToolDefinition> =
        offered.into_iter().map(|tool| (tool.name.to_string(), tool)).collect();
    let mut selected = BTreeMap::new();
    for name in &config.include {
        let definition = by_name.remove(name).ok_or_else(|| McpError::MissingTool(name.clone()))?;
        if let Some(annotations) = &definition.annotations
            && (annotations.read_only_hint == Some(false) || annotations.destructive_hint == Some(true))
        {
            return Err(McpError::NotReadOnly(name.clone()));
        }
        let parameters = Value::Object((*definition.input_schema).clone());
        let schema =
            ObjectSchema::parse(&parameters).map_err(|source| McpError::Schema { tool: name.clone(), source })?;
        let description = config
            .overrides
            .get(name)
            .map(|o| o.description.clone())
            .or_else(|| definition.description.as_ref().map(ToString::to_string))
            .unwrap_or_default();
        selected
            .insert(name.clone(), DiscoveredTool { name: name.clone(), description, parameters, schema, definition });
    }
    // Whatever is left was offered by the server and not allow-listed: never
    // advertised, never callable.
    if !by_name.is_empty() {
        tracing::info!(ignored = ?by_name.keys().collect::<Vec<_>>(), "MCP tools not in tools.mcp.include are not exposed");
    }
    Ok(selected)
}

/// Shared handle.
pub type SharedCatalog = Arc<McpCatalog>;

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;
    use crate::config::ToolOverride;

    fn tool(name: &str, schema: Value) -> McpToolDefinition {
        McpToolDefinition::new(
            name.to_string(),
            format!("{name} description"),
            Arc::new(schema.as_object().unwrap().clone()),
        )
    }

    fn config(include: &[&str]) -> McpToolsConfig {
        McpToolsConfig {
            function_group: "tickets_mcp".into(),
            tool_call_timeout_seconds: 30,
            include: include.iter().map(|s| s.to_string()).collect(),
            overrides: BTreeMap::from([("get_ticket".to_string(), ToolOverride { description: "Overridden.".into() })]),
        }
    }

    fn get_ticket() -> McpToolDefinition {
        tool(
            "get_ticket",
            json!({"type": "object", "properties": {"ticket_id": {"type": "string"}}, "required": ["ticket_id"]}),
        )
    }

    #[test]
    fn only_allow_listed_tools_are_selected_with_their_overrides() {
        let offered = vec![get_ticket(), tool("drop_database", json!({"type": "object"}))];
        let selected = select_tools(offered, &config(&["get_ticket"])).unwrap();
        assert_eq!(selected.keys().collect::<Vec<_>>(), vec!["get_ticket"]);
        assert_eq!(selected["get_ticket"].description, "Overridden.");
    }

    #[test]
    fn a_missing_allow_listed_tool_stops_startup() {
        assert!(matches!(
            select_tools(vec![get_ticket()], &config(&["get_ticket", "search_tickets"])),
            Err(McpError::MissingTool(_))
        ));
    }

    #[test]
    fn a_tool_taking_identity_arguments_is_refused() {
        let offered =
            vec![tool("get_ticket", json!({"type": "object", "properties": {"user_id": {"type": "string"}}}))];
        assert!(matches!(select_tools(offered, &config(&["get_ticket"])), Err(McpError::Schema { .. })));
    }

    #[test]
    fn a_destructive_tool_is_refused() {
        let mut destructive = get_ticket();
        destructive.annotations = Some(rmcp::model::ToolAnnotations::new().destructive(true));
        assert!(matches!(select_tools(vec![destructive], &config(&["get_ticket"])), Err(McpError::NotReadOnly(_))));
    }
}
