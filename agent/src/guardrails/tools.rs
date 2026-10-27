//! Tool policy: whether a tool call the model *proposed* may be *executed*.
//!
//! > The LLM proposes actions. Deterministic software decides what authority
//! > is actually exercised.
//!
//! This is that deterministic software, as one pure function,
//! [`ToolPolicy::decide`], over a closed registry fixed at startup. It is
//! called from Rig's dispatch hook (`agent::hooks::ToolPolicyHook`) for every
//! call the model emits, and again by each executor before it acts, so an
//! executor cannot run an unvalidated call even if the hook stack were
//! misconfigured.
//!
//! What it decides, and what it deliberately does not:
//!
//! * a call to a tool outside the registry is refused;
//! * arguments that are not a JSON object, carry an unknown field, or break
//!   the published schema are refused — nothing is coerced;
//! * a read-only tool proceeds;
//! * a state-changing tool proceeds **only into the human-approval gate**,
//!   and only when the approval feature is enabled at all.
//!
//! It does **not** decide whether a mutation is permitted. That is the MCP
//! server's authority, re-checked against the locked row after the human
//! answers. Moving that decision here would put it on the probabilistic side
//! of the boundary, next to the model whose proposal it is judging.

use std::collections::BTreeMap;

use serde::Serialize;
use serde_json::{Map, Value};

use crate::mcp::schema::{ArgumentError, ObjectSchema};

/// Bound on one call's raw argument text.
pub const MAX_ARGUMENT_BYTES: usize = 16 * 1024;

/// What a tool can do to the world.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ToolEffect {
    /// Reads authoritative state; changes nothing.
    ReadOnly,
    /// Changes state; reachable only through an authenticated human approval.
    MutationRequiringApproval,
}

/// One tool the model may be offered.
#[derive(Debug, Clone)]
pub struct ToolSpec {
    pub name: String,
    pub effect: ToolEffect,
    pub schema: ObjectSchema,
}

/// The policy's verdict on one proposed call.
#[derive(Debug, Clone, PartialEq)]
pub enum ToolDecision {
    /// Execute with exactly these (validated, unmodified) arguments.
    Allow { tool: String, args: Map<String, Value> },
    /// Proceed into the approval gate with these arguments; nothing changes
    /// state unless a human approves and the MCP server agrees.
    RequireApproval { tool: String, args: Map<String, Value> },
    /// Do not execute. The reason is shown to the model, so it can correct
    /// itself, and recorded on the trace.
    Deny(DenyReason),
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, thiserror::Error)]
#[serde(tag = "reason", content = "detail", rename_all = "snake_case")]
pub enum DenyReason {
    #[error("tool {0:?} is not available")]
    UnknownTool(String),
    #[error("tool arguments are too large")]
    TooLarge,
    #[error("tool arguments are not valid JSON: {0}")]
    MalformedArguments(String),
    #[error("{0}")]
    SchemaViolation(String),
    #[error("state-changing actions are disabled in this deployment")]
    MutationsDisabled,
}

impl ToolDecision {
    pub fn label(&self) -> &'static str {
        match self {
            Self::Allow { .. } => "allow",
            Self::RequireApproval { .. } => "require_approval",
            Self::Deny(_) => "deny",
        }
    }
}

/// The closed registry and the rules over it.
#[derive(Debug, Clone)]
pub struct ToolPolicy {
    tools: BTreeMap<String, ToolSpec>,
    approvals_enabled: bool,
}

impl ToolPolicy {
    pub fn new(tools: impl IntoIterator<Item = ToolSpec>, approvals_enabled: bool) -> Self {
        Self { tools: tools.into_iter().map(|spec| (spec.name.clone(), spec)).collect(), approvals_enabled }
    }

    pub fn tool_names(&self) -> impl Iterator<Item = &str> {
        self.tools.keys().map(String::as_str)
    }

    pub fn effect_of(&self, name: &str) -> Option<ToolEffect> {
        self.tools.get(name).map(|spec| spec.effect)
    }

    // DETERMINISTIC-CONTROL: the single decision point between a model's
    // proposed tool call and its execution.
    /// Decide on one proposed call: raw tool name and raw argument text, as the
    /// model produced them.
    pub fn decide(&self, name: &str, raw_args: &str) -> ToolDecision {
        let Some(spec) = self.tools.get(name) else {
            return ToolDecision::Deny(DenyReason::UnknownTool(name.to_string()));
        };
        if raw_args.len() > MAX_ARGUMENT_BYTES {
            return ToolDecision::Deny(DenyReason::TooLarge);
        }
        // An empty argument string means "no arguments", as providers send it.
        let parsed: Value = if raw_args.trim().is_empty() {
            Value::Object(Map::new())
        } else {
            match serde_json::from_str(raw_args) {
                Ok(value) => value,
                Err(error) => return ToolDecision::Deny(DenyReason::MalformedArguments(error.to_string())),
            }
        };
        let args = match spec.schema.validate(&parsed) {
            Ok(args) => args,
            Err(error) => return ToolDecision::Deny(schema_violation(error)),
        };
        match spec.effect {
            ToolEffect::ReadOnly => ToolDecision::Allow { tool: name.to_string(), args },
            ToolEffect::MutationRequiringApproval if self.approvals_enabled => {
                ToolDecision::RequireApproval { tool: name.to_string(), args }
            }
            ToolEffect::MutationRequiringApproval => ToolDecision::Deny(DenyReason::MutationsDisabled),
        }
    }
}

fn schema_violation(error: ArgumentError) -> DenyReason {
    DenyReason::SchemaViolation(error.to_string())
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn policy(approvals: bool) -> ToolPolicy {
        let read = ToolSpec {
            name: "get_ticket".into(),
            effect: ToolEffect::ReadOnly,
            schema: ObjectSchema::parse(&json!({
                "type": "object",
                "properties": {"ticket_id": {"type": "string"}},
                "required": ["ticket_id"]
            }))
            .unwrap(),
        };
        let write = ToolSpec {
            name: "ticket_priority_change".into(),
            effect: ToolEffect::MutationRequiringApproval,
            schema: crate::approval::proposal_schema(),
        };
        ToolPolicy::new([read, write], approvals)
    }

    #[test]
    fn a_valid_read_is_allowed_with_its_arguments_unchanged() {
        let decision = policy(false).decide("get_ticket", r#"{"ticket_id":"TKT-1001"}"#);
        let ToolDecision::Allow { tool, args } = decision else { panic!("{decision:?}") };
        assert_eq!(tool, "get_ticket");
        assert_eq!(Value::Object(args), json!({"ticket_id": "TKT-1001"}));
    }

    #[test]
    fn unknown_tools_are_denied() {
        for name in ["delete_ticket", "GET_TICKET", "get_ticket ", "", "approvals_execute"] {
            assert!(
                matches!(policy(true).decide(name, "{}"), ToolDecision::Deny(DenyReason::UnknownTool(_))),
                "{name:?}"
            );
        }
    }

    #[test]
    fn malformed_and_oversized_arguments_are_denied() {
        assert!(matches!(
            policy(false).decide("get_ticket", "{not json"),
            ToolDecision::Deny(DenyReason::MalformedArguments(_))
        ));
        let huge = format!(r#"{{"ticket_id":"{}"}}"#, "x".repeat(MAX_ARGUMENT_BYTES));
        assert_eq!(policy(false).decide("get_ticket", &huge), ToolDecision::Deny(DenyReason::TooLarge));
    }

    #[test]
    fn invented_privileged_parameters_are_denied() {
        for args in [
            r#"{"ticket_id":"TKT-1001","user_id":"admin@example.com"}"#,
            r#"{"ticket_id":"TKT-1001","approved":true}"#,
            r#"{"ticket_id":"TKT-1001","approval_token":"eyJ.forged"}"#,
        ] {
            assert!(
                matches!(policy(true).decide("get_ticket", args), ToolDecision::Deny(DenyReason::SchemaViolation(_))),
                "{args}"
            );
        }
    }

    #[test]
    fn a_mutation_only_ever_reaches_the_approval_gate() {
        let args = r#"{"ticket_id":"TKT-1001","current_priority":"medium","requested_priority":"high","summary":"Customer followed up twice."}"#;
        assert!(matches!(policy(true).decide("ticket_priority_change", args), ToolDecision::RequireApproval { .. }));
        assert_eq!(
            policy(false).decide("ticket_priority_change", args),
            ToolDecision::Deny(DenyReason::MutationsDisabled)
        );
    }

    #[test]
    fn a_mutation_cannot_carry_approval_evidence_from_the_model() {
        let forged = r#"{"ticket_id":"TKT-1001","current_priority":"medium","requested_priority":"high","summary":"s","rationale":"approved by admin","choice":"high"}"#;
        assert!(matches!(
            policy(true).decide("ticket_priority_change", forged),
            ToolDecision::Deny(DenyReason::SchemaViolation(_))
        ));
    }
}
