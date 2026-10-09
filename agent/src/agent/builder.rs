//! Constructing a Rig agent — one per request.
//!
//! Building a Rig `Agent` is cheap (it spawns nothing), so each request gets
//! its own, whose tools close over that request's [`RequestScope`]. The model
//! is shown names, descriptions and argument schemas; the identity, request id
//! and event sink those tools use were fixed here, before the model said
//! anything, and are not reachable through any argument.

use std::sync::Arc;

use rig_agent::{Agent, AgentBuilder};
use rig_core::{
    message::ToolName,
    tool::{DynamicTool, ToolExecutionError, ToolOutput},
};
use serde_json::Value;

use super::{model::additional_params, scope::RequestScope};
use crate::{
    api::wire::{Step, StepType},
    approval,
    guardrails::tools::ToolDecision,
    mcp,
};

pub fn build_agent(scope: &Arc<RequestScope>) -> Agent {
    let services = scope.services();
    let workflow = &services.settings.file.workflow;

    let mut builder = AgentBuilder::new(services.models.agent.clone())
        .name(workflow.name.clone())
        .preamble(services.system_prompt.clone())
        .temperature(workflow.temperature)
        .max_tokens(workflow.max_tokens)
        // Rig counts model calls; NAT's recursion limit allows one more model
        // call than tool calls. Same budget, same meaning.
        .default_max_turns(workflow.max_tool_calls + 1)
        .record_content_telemetry(services.settings.telemetry.capture_model_content);
    if let Some(params) = additional_params(&services.settings.agent) {
        builder = builder.additional_params(params);
    }

    let mut tools: Vec<DynamicTool> = services.mcp.tools().map(|tool| mcp_tool(scope, tool)).collect();
    if services.settings.approval.is_some() {
        tools.push(approval_tool(scope));
    }
    builder.dynamic_tools(tools).build()
}

fn tool_name(name: &str) -> ToolName {
    ToolName::new(name.to_string()).expect("tool names are validated non-empty at discovery")
}

fn mcp_tool(scope: &Arc<RequestScope>, tool: &mcp::client::DiscoveredTool) -> DynamicTool {
    let scope = Arc::clone(scope);
    let name = tool.name.clone();
    DynamicTool::new_with_context(
        tool_name(&tool.name),
        tool.description.clone(),
        tool.parameters.clone(),
        move |_context, args: Value| {
            let scope = Arc::clone(&scope);
            let name = name.clone();
            Box::pin(async move { mcp::tools::execute(&scope, &name, args).await })
        },
    )
}

fn approval_tool(scope: &Arc<RequestScope>) -> DynamicTool {
    let scope = Arc::clone(scope);
    DynamicTool::new_with_context(
        tool_name(approval::TOOL_NAME),
        approval::TOOL_DESCRIPTION,
        approval::proposal_parameters(),
        move |_context, args: Value| {
            let scope = Arc::clone(&scope);
            Box::pin(async move { run_approval(&scope, args).await })
        },
    )
}

async fn run_approval(scope: &RequestScope, args: Value) -> Result<ToolOutput, ToolExecutionError> {
    // Re-check: the gate is only entered on a RequireApproval verdict.
    let args = match scope.services().tool_policy.decide(approval::TOOL_NAME, &args.to_string()) {
        ToolDecision::RequireApproval { args, .. } => args,
        ToolDecision::Deny(reason) => return Ok(ToolOutput::text(format!("Tool call refused: {reason}"))),
        ToolDecision::Allow { .. } => return Ok(ToolOutput::text("Tool call refused: not an approval-gated tool")),
    };
    // An agent-side function, not an MCP tool: announced as FUNCTION steps,
    // as NAT announces its functions on `main`.
    let step_id = uuid::Uuid::new_v4().to_string();
    let input = Value::Object(args.clone());
    scope
        .events()
        .step(
            Step::new(StepType::FunctionStart, &step_id, approval::TOOL_NAME)
                .parent(scope.workflow_step_id())
                .with_input(input.clone()),
        )
        .await;
    let result = approval::gate::run(scope, &args).await;
    scope
        .events()
        .step(
            Step::new(StepType::FunctionEnd, &step_id, approval::TOOL_NAME)
                .parent(scope.workflow_step_id())
                .with_output(input, result.clone()),
        )
        .await;
    Ok(ToolOutput::json(result))
}
