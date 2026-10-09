//! What this agent is, so an evaluation result can name it (`GET /version`).
//!
//! Same purpose and fields as `main`'s `provenance.py`, plus the one that
//! matters most on this branch: `agent_runtime: "rig-rust"`. Digests, never
//! prompt text; model names, never credentials.

use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::{approval::token::canonical_json, config::Settings, guardrails::tools::ToolPolicy};

pub const AGENT_RUNTIME: &str = "rig-rust";
pub const RIG_VERSION: &str = "0.44.0";
pub const RMCP_VERSION: &str = "2.2.0";

fn sha256_hex(bytes: &[u8]) -> String {
    Sha256::digest(bytes).iter().map(|byte| format!("{byte:02x}")).collect()
}

pub fn describe(settings: &Settings, policy: &ToolPolicy) -> Value {
    let raw = std::fs::read(&settings.config_path).ok();
    let prompts = serde_json::to_value(&settings.file.guardrails.prompts).unwrap_or(Value::Null);
    let mut tools: Vec<&str> = policy.tool_names().collect();
    tools.sort_unstable();
    json!({
        "available": raw.is_some(),
        "agent_runtime": AGENT_RUNTIME,
        "agent_framework": "rig",
        "agent_framework_version": RIG_VERSION,
        "mcp_sdk": "rmcp",
        "mcp_sdk_version": RMCP_VERSION,
        "agent_version": env!("CARGO_PKG_VERSION"),
        "model": settings.agent.model,
        "guard_model": settings.guard.model,
        // Empty means "omitted from the request", as on `main`.
        "reasoning_effort": settings.agent.reasoning_effort.clone().unwrap_or_default(),
        "guard_reasoning_effort": settings.guard.reasoning_effort.clone().unwrap_or_default(),
        "build_commit": settings.build_commit,
        "config_path": settings.config_path.display().to_string(),
        "config_sha256": raw.as_deref().map(sha256_hex),
        // The template text, before tool rendering: identical to `main`'s
        // prompt, so this digest matches across the two branches.
        "prompt_sha256": sha256_hex(settings.file.workflow.system_prompt.as_bytes()),
        "guardrails_prompts_sha256": sha256_hex(canonical_json(&prompts).as_bytes()),
        "guardrails_prompts_digest": "canonical-json",
        "tools_exposed": tools,
    })
}
