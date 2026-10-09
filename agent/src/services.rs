//! Process-wide services, built once at startup and shared by every request.

use std::{sync::Arc, time::Duration};

use serde_json::Value;

use crate::{
    agent::model::{ChatModel, chat_model},
    approval::{self, pending::InteractionRegistry},
    config::Settings,
    guardrails::{
        output::{OutputPolicy, OutputPolicyError},
        tools::{ToolEffect, ToolPolicy, ToolSpec},
    },
    mcp::client::{McpCatalog, McpError},
    telemetry::provenance,
};

pub struct Models {
    /// The agent model: the one probabilistic decision maker in the loop.
    pub agent: ChatModel,
    /// The input classifier: a separate call, with its own prompt.
    pub guard: ChatModel,
}

pub struct Services {
    pub settings: Settings,
    pub models: Models,
    pub mcp: McpCatalog,
    pub tool_policy: ToolPolicy,
    pub output_policy: OutputPolicy,
    pub interactions: Arc<InteractionRegistry>,
    pub http: reqwest::Client,
    /// The system prompt with `{tools}` and `{tool_names}` filled in.
    pub system_prompt: String,
    pub provenance: Value,
}

#[derive(Debug, thiserror::Error)]
pub enum StartupError {
    #[error(transparent)]
    Mcp(#[from] McpError),
    #[error(transparent)]
    OutputPolicy(#[from] OutputPolicyError),
}

impl Services {
    /// Connect to MCP, discover and check tools, compile every policy.
    /// Retries the MCP connection briefly, then fails: a container restart
    /// policy is a better retry loop than a half-started agent.
    pub async fn start(settings: Settings) -> Result<Arc<Self>, StartupError> {
        let mut attempt = 0;
        let mcp = loop {
            match McpCatalog::discover(&settings.mcp_url, &settings.mcp_api_key, &settings.file.tools.mcp).await {
                Ok(catalog) => break catalog,
                Err(McpError::Connect(reason)) if attempt < 15 => {
                    attempt += 1;
                    tracing::warn!(attempt, %reason, "MCP server not reachable yet; retrying");
                    tokio::time::sleep(Duration::from_secs(2)).await;
                }
                Err(error) => return Err(error.into()),
            }
        };

        let approvals_enabled = settings.approval.is_some();
        let mut specs: Vec<ToolSpec> = mcp
            .tools()
            .map(|tool| ToolSpec { name: tool.name.clone(), effect: ToolEffect::ReadOnly, schema: tool.schema.clone() })
            .collect();
        if approvals_enabled {
            specs.push(ToolSpec {
                name: approval::TOOL_NAME.into(),
                effect: ToolEffect::MutationRequiringApproval,
                schema: approval::proposal_schema(),
            });
        }
        let tool_policy = ToolPolicy::new(specs, approvals_enabled);

        let output = &settings.file.guardrails.output;
        let output_policy =
            OutputPolicy::new(&output.secret_patterns, &output.pii_entities, settings.guardrails.max_answer_chars)?;

        let system_prompt = render_system_prompt(&settings, &mcp);
        let provenance = provenance::describe(&settings, &tool_policy);
        let models = Models { agent: chat_model(&settings.agent), guard: chat_model(&settings.guard) };

        Ok(Arc::new(Self {
            settings,
            models,
            mcp,
            tool_policy,
            output_policy,
            interactions: Arc::new(InteractionRegistry::default()),
            http: reqwest::Client::builder().connect_timeout(Duration::from_secs(5)).build().unwrap_or_default(),
            system_prompt,
            provenance,
        }))
    }
}

/// Fill the prompt's `{tools}` and `{tool_names}` placeholders the way NAT's
/// ReAct agent does: one `name: description` line per tool, and a
/// comma-separated list of names.
pub fn render_system_prompt(settings: &Settings, mcp: &McpCatalog) -> String {
    let mut tools: Vec<(String, String)> = mcp.tools().map(|t| (t.name.clone(), t.description.clone())).collect();
    if settings.approval.is_some() {
        tools.push((approval::TOOL_NAME.into(), approval::TOOL_DESCRIPTION.into()));
    }
    let lines = tools.iter().map(|(name, description)| format!("{name}: {description}")).collect::<Vec<_>>().join("\n");
    let names = tools.iter().map(|(name, _)| name.as_str()).collect::<Vec<_>>().join(", ");
    settings.file.workflow.system_prompt.replace("{tools}", &lines).replace("{tool_names}", &names)
}
