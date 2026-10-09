//! The executor behind each MCP-backed Rig tool.
//!
//! Rig decides *when* to run a tool (the model asked, the policy hook let the
//! call through); this decides *how*: re-check the call against the same
//! policy, announce it on the stream, call MCP, report the result. The re-check
//! is deliberate redundancy — an executor must not depend on a hook configured
//! elsewhere in order to refuse an unvalidated call.

use rig_core::tool::{ToolExecutionError, ToolOutput};
use serde_json::{Value, json};
use tracing::Instrument;

use crate::{
    agent::scope::RequestScope,
    api::wire::{Step, StepType},
    guardrails::tools::ToolDecision,
};

/// The name tool steps and spans carry: `<function_group>__<tool>`, as on `main`.
pub fn step_name(scope: &RequestScope, tool: &str) -> String {
    format!("{}__{tool}", scope.services().settings.file.tools.mcp.function_group)
}

/// Run one MCP tool call proposed by the model.
pub async fn execute(scope: &RequestScope, tool: &str, args: Value) -> Result<ToolOutput, ToolExecutionError> {
    let decision = scope.services().tool_policy.decide(tool, &args.to_string());
    let args = match decision {
        ToolDecision::Allow { args, .. } => args,
        // The hook should have stopped this already; answer exactly as it would.
        ToolDecision::Deny(reason) => return Ok(ToolOutput::text(format!("Tool call refused: {reason}"))),
        ToolDecision::RequireApproval { .. } => {
            return Ok(ToolOutput::text("Tool call refused: this tool is not a read-only MCP tool"));
        }
    };

    let name = step_name(scope, tool);
    let step_id = uuid::Uuid::new_v4().to_string();
    let input = Value::Object(args.clone());
    let span = tracing::info_span!(
        "mcp.tool",
        otel.name = %name,
        otel.kind = "client",
        mcp.tool.name = tool,
        mcp.method = "tools/call",
        tool.outcome = tracing::field::Empty,
        input.value = tracing::field::Empty,
        output.value = tracing::field::Empty,
    );
    let capture = scope.services().settings.telemetry.capture_content;
    let limit = scope.services().settings.telemetry.content_max_chars;
    if capture {
        span.record("input.value", crate::telemetry::bounded(&input.to_string(), limit).as_str());
    }

    scope
        .events()
        .step(
            Step::new(StepType::ToolStart, &step_id, &name).parent(scope.workflow_step_id()).with_input(input.clone()),
        )
        .await;

    let result = scope.services().mcp.call(tool, &args).instrument(span.clone()).await;

    let (output, model_output) = match result {
        Ok(output) => {
            span.record("tool.outcome", "ok");
            (tool_output_json(&output), output)
        }
        Err(error) => {
            span.record("tool.outcome", "error");
            let message = error.to_string();
            // Passed back to the model, which may correct itself — the same
            // as `pass_tool_call_errors_to_agent: true` on `main`.
            (json!({ "error": message }), ToolOutput::text(format!("Error: {message}")))
        }
    };
    // The model reasons over the raw result; the client's tool card and the
    // trace get a copy with secrets redacted and PII masked.
    let shown = scope.services().output_policy.redact_for_display(&output);
    if capture {
        span.record("output.value", crate::telemetry::bounded(&shown.to_string(), limit).as_str());
    }
    scope
        .events()
        .step(Step::new(StepType::ToolEnd, &step_id, &name).parent(scope.workflow_step_id()).with_output(input, shown))
        .await;
    Ok(model_output)
}

/// A tool output as JSON for the step event: the server returns JSON as a text
/// block, which the evaluator grounds answers against, so decode it when it is
/// JSON and keep it as text when it is not.
fn tool_output_json(output: &ToolOutput) -> Value {
    if let Some(value) = output.as_json() {
        return value.clone();
    }
    let text = output.render();
    serde_json::from_str(&text).unwrap_or(Value::String(text))
}
