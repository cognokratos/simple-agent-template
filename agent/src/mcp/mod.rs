//! The MCP capability boundary, from the agent's side.
//!
//! * [`client`] — session, discovery, allow-list, invocation (over `rig-rmcp`);
//! * [`schema`] — the strict argument schemas every call is checked against;
//! * [`tools`] — the executor each Rig tool calls: policy re-check, step
//!   events, span, MCP call.

pub mod client;
pub mod schema;
pub mod tools;
