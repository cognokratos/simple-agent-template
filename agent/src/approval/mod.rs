//! Human approval for state-changing actions — optional, off by default.
//!
//! The model may *propose* a change by calling `ticket_priority_change`. That
//! proposal is the only part the model controls. Everything after it is
//! deterministic and outside the model's reach:
//!
//! ```text
//! model proposes            (probabilistic: tool arguments)
//!   → tool policy           guardrails::tools — schema, closed object, feature switch
//!   → approval gate         approval::gate — suspends the run on a prompt
//!   → human answers         api::interactions — authenticated, CSRF-checked upstream
//!   → registry checks       approval::pending — owner, prompt kind, offered pair
//!   → token minted          approval::token — bound to actor, request, state shown
//!   → MCP verifies + applies   mcp-server — signature, bindings, row lock, policy,
//!                               nonce, audit, one transaction
//!   → model told the outcome    it never restates what was approved
//! ```
//!
//! The same tool name, argument schema, prompts, token format and MCP endpoint
//! as `main`'s `approval.py`, so the UI, the gateway and the MCP server are
//! unchanged by this branch.

pub mod execute;
pub mod gate;
pub mod pending;
pub mod token;

use serde_json::{Value, json};

use crate::mcp::schema::ObjectSchema;

/// The tool the model sees.
pub const TOOL_NAME: &str = "ticket_priority_change";
/// The action the MCP server's registry knows (`mutation::ACTIONS`).
pub const ACTION: &str = "set_ticket_priority";
/// The MCP server's `allowed_choices` for that action, in display order.
pub const PRIORITIES: [&str; 4] = ["low", "medium", "high", "urgent"];

pub const TOOL_DESCRIPTION: &str = "Change a ticket's priority. Requires explicit human approval: the user is \
shown the current priority and chooses what to record, and any change requires them to type a reason. Call it \
with the ticket's exact current priority from get_ticket. You cannot approve on the user's behalf, and you must \
never claim a change was applied unless the result says committed.";

/// The argument schema the model is shown, identical in content to `main`'s
/// `SetTicketPriorityRequest`. There is no field for a decision, an actor, a
/// rationale or a token, and the object is closed, so none can be supplied.
pub fn proposal_parameters() -> Value {
    json!({
        "type": "object",
        "properties": {
            "ticket_id": {
                "type": "string",
                "minLength": 1,
                "description": "Exact ticket identifier, e.g. TKT-1001"
            },
            "current_priority": {
                "type": "string",
                "enum": PRIORITIES,
                "description": "The ticket's current priority, exactly as get_ticket returned it"
            },
            "requested_priority": {
                "type": "string",
                "enum": PRIORITIES,
                "description": "The priority to apply. Your recommendation only; the user chooses, and any change away from the current priority requires them to type a reason"
            },
            "summary": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1500,
                "description": "Concise user-facing explanation of the proposed change"
            },
            "note": {
                "type": ["string", "null"],
                "maxLength": 4000,
                "description": "Optional grounded note to persist with the decision, drawn from the ticket and its history"
            }
        },
        "required": ["ticket_id", "current_priority", "requested_priority", "summary"],
        "additionalProperties": false
    })
}

pub fn proposal_schema() -> ObjectSchema {
    ObjectSchema::parse(&proposal_parameters()).expect("the approval schema is in the supported subset")
}
