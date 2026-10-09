//! Rig-based agent service for the support-ticket template (`rust-agent` branch).
//!
//! The canonical implementation of this service is the NeMo Agent Toolkit
//! agent on `main`. This crate implements the same agent-service contract
//! (docs/AGENT-SERVICE-CONTRACT.md) in Rust, with Rig as the agent framework,
//! so the two can be compared with everything around them held fixed.
//!
//! > Intelligence does not imply authority.
//!
//! The model proposes; this crate decides. The modules are arranged so that
//! the two are easy to tell apart:
//!
//! * **probabilistic** — the agent model's turns (inside Rig, `agent`), and
//!   the input classifier's reply (`agent::model::classify`);
//! * **deterministic** — everything else: [`api::auth`] (who is calling),
//!   [`guardrails`] (what may go in, run, and come out), [`approval`] (what a
//!   human authorised), [`mcp`] (what a tool may be asked).

pub mod agent;
pub mod api;
pub mod approval;
pub mod config;
pub mod error;
pub mod guardrails;
pub mod identity;
pub mod mcp;
pub mod services;
pub mod telemetry;
