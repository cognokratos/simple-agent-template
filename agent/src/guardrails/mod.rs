//! Policy layers around the model, each explicit about what kind of decision
//! it makes:
//!
//! | Module | Decides | Kind |
//! | --- | --- | --- |
//! | [`input`] | whether a request reaches the agent model | deterministic patterns + one **probabilistic** classifier call, deterministically combined |
//! | [`tools`] | whether a proposed tool call executes | deterministic |
//! | [`output`] | what text leaves the service | deterministic |
//! | [`pii`] | which spans of output are masked | deterministic |
//!
//! Nothing here decides whether a *mutation* is permitted: that authority is
//! the MCP server's, re-checked at the point of mutation.

pub mod input;
pub mod output;
pub mod pii;
pub mod tools;
