//! The Rig hook where deterministic software decides on every model-proposed
//! tool call.
//!
//! Rig's agent loop calls [`AgentHook::on_dispatch`] for each tool call the
//! model emits, *before* the tool runs, with the raw tool name and the raw
//! argument JSON exactly as the model produced them. This hook hands both to
//! [`ToolPolicy::decide`] and turns the verdict into Rig's answer:
//!
//! * `Allow` / `RequireApproval` → `Proceed` (an approval-requiring call
//!   proceeds only *into the approval gate*, which changes nothing by itself);
//! * `Deny` → `skip`: the tool does not run, and the model sees the reason as
//!   the tool's result, so it can correct its call.
//!
//! [`AgentHook::on_invalid_tool_call`] covers what Rig catches before
//! dispatch — a tool name that was never advertised, or arguments that are not
//! a JSON object — with the same deterministic skip.

use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};

use rig_agent::{
    AgentHook, HookContext,
    agent::{DispatchAction, DispatchEvent},
    run::{InvalidToolCallAction, InvalidToolCallContext, InvalidToolCallReason},
};

use crate::{
    agent::scope::RequestScope,
    guardrails::tools::{DenyReason, ToolDecision},
};

pub struct ToolPolicyHook {
    scope: Arc<RequestScope>,
    /// Tool calls dispatched so far in this run.
    dispatched: AtomicUsize,
    /// `workflow.max_tool_calls`.
    budget: usize,
}

impl ToolPolicyHook {
    pub fn new(scope: Arc<RequestScope>) -> Self {
        let budget = scope.services().settings.file.workflow.max_tool_calls;
        Self { scope, dispatched: AtomicUsize::new(0), budget }
    }
}

fn record_decision(tool: &str, decision: &str, reason: Option<&str>) {
    // One span per decision, so the trace shows each proposal and verdict
    // beside the tool span it led to (or did not).
    let span = tracing::info_span!(
        "tool.policy",
        tool.name = tool,
        policy.decision = decision,
        policy.reason = reason.unwrap_or(""),
        policy.kind = "deterministic",
    );
    let _entered = span.enter();
    if decision == "deny" {
        tracing::warn!(tool, reason = reason.unwrap_or(""), "tool call refused by policy");
    }
}

impl AgentHook for ToolPolicyHook {
    fn name(&self) -> Option<String> {
        Some("tool-policy".into())
    }

    async fn on_dispatch(&self, _ctx: &HookContext, event: DispatchEvent<'_>) -> DispatchAction {
        // Completion dispatches (model calls) pass through untouched.
        let (Some(tool), Some(args)) = (event.tool_name(), event.tool_args()) else {
            return DispatchAction::Proceed;
        };
        // DETERMINISTIC-CONTROL: the tool budget. Rig bounds *model* calls; this
        // bounds *tool* calls, so `max_tool_calls: 20` means twenty, even when
        // the last permitted model turn asks for more.
        if self.dispatched.fetch_add(1, Ordering::SeqCst) >= self.budget {
            record_decision(tool, "deny", Some("tool-call budget exhausted"));
            return DispatchAction::skip(format!(
                "Tool call refused by policy: the budget of {} tool calls is exhausted; answer with what you have",
                self.budget
            ));
        }
        let decision = self.scope.services().tool_policy.decide(tool, args);
        match decision {
            ToolDecision::Allow { .. } | ToolDecision::RequireApproval { .. } => {
                record_decision(tool, decision.label(), None);
                DispatchAction::Proceed
            }
            ToolDecision::Deny(reason) => {
                let reason = reason.to_string();
                record_decision(tool, "deny", Some(&reason));
                DispatchAction::skip(format!("Tool call refused by policy: {reason}"))
            }
        }
    }

    async fn on_invalid_tool_call(
        &self,
        _ctx: &HookContext,
        event: &InvalidToolCallContext,
    ) -> Option<InvalidToolCallAction> {
        let reason = match &event.reason {
            InvalidToolCallReason::MalformedArguments { error } => DenyReason::MalformedArguments(error.clone()),
            _ => DenyReason::UnknownTool(event.tool_name.clone()),
        }
        .to_string();
        record_decision(&event.tool_name, "deny", Some(&reason));
        Some(InvalidToolCallAction::skip(format!("Tool call refused by policy: {reason}")))
    }
}
