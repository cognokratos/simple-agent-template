//! The agent: a Rig agent loop wrapped in explicit, deterministic policy.
//!
//! Start at [`execution`], which walks one request through every step.
//!
//! | Module | Role |
//! | --- | --- |
//! | [`execution`] | the request lifecycle, step by step |
//! | [`builder`] | assembling the per-request Rig agent and its tools |
//! | [`hooks`] | Rig's dispatch hook — where tool policy is enforced |
//! | [`input_rail`] | running the input policy and its classifier call |
//! | [`model`] | OpenAI-compatible models through Rig |
//! | [`scope`] | what one request's tools are bound to |
//! | [`state`] | the execution state machine |

pub mod builder;
pub mod execution;
pub mod hooks;
pub mod input_rail;
pub mod model;
pub mod scope;
pub mod state;
