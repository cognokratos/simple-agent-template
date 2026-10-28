//! What this agent is, so an evaluation result — and every trace — can name it
//! (`GET /version`, and the root span's `agent.*` attributes).
//!
//! Includes `agent_runtime: "rig-rust"`, so results from the retired NAT
//! runtime (which predates the field) stay distinguishable. Digests, never
//! prompt text; model names, never credentials; an endpoint *fingerprint*,
//! never the endpoint URL.
//!
//! Two prompt digests, on purpose:
//!
//! * `prompt_sha256` — the template in `agent/config.yml`, before `{tools}` and
//!   `{tool_names}` are filled. Stable across MCP catalogue changes, and
//!   comparable with the file without a running agent.
//! * `rendered_prompt_sha256` — the system prompt the model actually receives,
//!   after MCP discovery. Together with `tool_catalog_sha256` it identifies the
//!   model-visible capability surface exactly.
//!
//! `agent_revision_sha256` is one digest over the agent's complete behavioural
//! identity: the fields named in [`REVISION_FIELDS`] and nothing else. It does
//! not include dataset or gate settings; those are the evaluation's, and live
//! in the evaluator's `conditions.sha256`.

use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::{approval::token::canonical_json, config::Settings, guardrails::tools::ToolPolicy};

pub const AGENT_RUNTIME: &str = "rig-rust";
pub const AGENT_FRAMEWORK: &str = "rig";
pub const RIG_VERSION: &str = "0.44.0";
pub const RMCP_VERSION: &str = "2.2.0";

/// Exactly what `agent_revision_sha256` digests, in the documented order.
/// Changing this list changes every revision digest, so it is part of the
/// contract (docs/OBSERVABILITY.md#agent-revision).
pub const REVISION_FIELDS: &[&str] = &[
    "agent_runtime",
    "agent_framework",
    "agent_framework_version",
    "mcp_sdk_version",
    "agent_version",
    "build_commit",
    "config_sha256",
    "rendered_prompt_sha256",
    "tool_catalog_sha256",
    "guardrails_prompts_sha256",
    "behaviour_settings_sha256",
    "model",
    "guard_model",
    "reasoning_effort",
    "guard_reasoning_effort",
    "guard_temperature",
    "provider",
    "endpoint_sha256",
];

pub fn sha256_hex(bytes: &[u8]) -> String {
    Sha256::digest(bytes).iter().map(|byte| format!("{byte:02x}")).collect()
}

/// A fingerprint of the model endpoint that can be compared but not read.
///
/// The URL is normalised first — userinfo, query and fragment removed, scheme
/// and host lowercased, a trailing `/` dropped — so a credential embedded in
/// the URL never reaches the digest and cosmetic differences do not split one
/// endpoint into two. This identifies *which* endpoint served a model name; it
/// is not a secret-protection mechanism, since a short local URL can be
/// guessed and hashed by anyone.
pub fn endpoint_sha256(base_url: &str) -> String {
    let trimmed = base_url.trim();
    let without_fragment = trimmed.split('#').next().unwrap_or_default();
    let without_query = without_fragment.split('?').next().unwrap_or_default();
    let (scheme, rest) = without_query.split_once("://").unwrap_or(("", without_query));
    let (authority, path) = match rest.find('/') {
        Some(index) => rest.split_at(index),
        None => (rest, ""),
    };
    let host = authority.rsplit_once('@').map_or(authority, |(_, host)| host);
    let normalised =
        format!("{}://{}{}", scheme.to_ascii_lowercase(), host.to_ascii_lowercase(), path.trim_end_matches('/'));
    sha256_hex(normalised.as_bytes())
}

/// The model-visible tool catalogue, in canonical (name) order.
///
/// Every field that can change what the model does with a tool: its name, the
/// description it is shown, its argument JSON schema, and the deterministic
/// effect classification the policy applies to it.
pub fn tool_catalog(entries: impl IntoIterator<Item = (String, String, Value, &'static str)>) -> Value {
    let mut tools: Vec<Value> = entries
        .into_iter()
        .map(|(name, description, parameters, effect)| {
            json!({"name": name, "description": description, "parameters": parameters, "effect": effect})
        })
        .collect();
    tools.sort_by(|a, b| a["name"].as_str().cmp(&b["name"].as_str()));
    Value::Array(tools)
}

/// Settings that change decisions but live in the environment rather than in
/// `agent/config.yml` (whose bytes `config_sha256` already covers). Trace and
/// logging switches are excluded: they change what is recorded, not what the
/// agent does.
fn behaviour_settings(settings: &Settings) -> Value {
    let guardrails = &settings.guardrails;
    json!({
        "guardrails.deterministic_fallback": guardrails.deterministic_fallback,
        "guardrails.read_only_allow_override": guardrails.read_only_allow_override,
        "guardrails.block_message": guardrails.block_message,
        "guardrails.input_max_chars": guardrails.input_max_chars,
        "guardrails.oversize_message": guardrails.oversize_message,
        "guardrails.max_answer_chars": guardrails.max_answer_chars,
        "approvals.enabled": settings.approval.is_some(),
        "approvals.token_ttl_seconds": settings.approval.as_ref().map(|approval| approval.token_ttl_seconds),
        "approvals.interaction_timeout_seconds":
            settings.approval.as_ref().map(|approval| approval.interaction_timeout.as_secs()),
    })
}

pub fn describe(settings: &Settings, policy: &ToolPolicy, rendered_prompt: &str, catalog: &Value) -> Value {
    let raw = std::fs::read(&settings.config_path).ok();
    let prompts = serde_json::to_value(&settings.file.guardrails.prompts).unwrap_or(Value::Null);
    let mut tools: Vec<&str> = policy.tool_names().collect();
    tools.sort_unstable();
    let workflow = &settings.file.workflow;
    let mut record = json!({
        "available": raw.is_some(),
        "agent_name": workflow.name,
        "agent_runtime": AGENT_RUNTIME,
        "agent_framework": AGENT_FRAMEWORK,
        "agent_framework_version": RIG_VERSION,
        "mcp_sdk": "rmcp",
        "mcp_sdk_version": RMCP_VERSION,
        "agent_version": env!("CARGO_PKG_VERSION"),
        "service_version": settings.telemetry.service_version,
        "model": settings.agent.model,
        "guard_model": settings.guard.model,
        // Empty means "omitted from the request".
        "reasoning_effort": settings.agent.reasoning_effort.clone().unwrap_or_default(),
        "guard_reasoning_effort": settings.guard.reasoning_effort.clone().unwrap_or_default(),
        "temperature": workflow.temperature,
        "max_tokens": workflow.max_tokens,
        "guard_temperature": crate::agent::model::GUARD_TEMPERATURE,
        // Null when LLM_PROVIDER is unset: an absent name is not guessed.
        "provider": settings.agent.provider,
        "endpoint_sha256": endpoint_sha256(&settings.agent.base_url),
        "guard_endpoint_sha256": endpoint_sha256(&settings.guard.base_url),
        "build_commit": settings.build_commit,
        "config_path": settings.config_path.display().to_string(),
        "config_sha256": raw.as_deref().map(sha256_hex),
        // The template text, before tool rendering, so the digest does not
        // change when the MCP catalogue does.
        "prompt_sha256": sha256_hex(workflow.system_prompt.as_bytes()),
        // The system prompt as the model receives it.
        "rendered_prompt_sha256": sha256_hex(rendered_prompt.as_bytes()),
        "tool_catalog_sha256": sha256_hex(canonical_json(catalog).as_bytes()),
        "tool_catalog_digest": "canonical-json",
        "guardrails_prompts_sha256": sha256_hex(canonical_json(&prompts).as_bytes()),
        "guardrails_prompts_digest": "canonical-json",
        "behaviour_settings_sha256": sha256_hex(canonical_json(&behaviour_settings(settings)).as_bytes()),
        "tools_exposed": tools,
    });
    let revision: Map<String, Value> =
        REVISION_FIELDS.iter().map(|field| ((*field).to_string(), record[*field].clone())).collect();
    record["agent_revision_sha256"] = Value::String(sha256_hex(canonical_json(&Value::Object(revision)).as_bytes()));
    record["agent_revision_fields"] = json!(REVISION_FIELDS);
    record
}

/// The sanitized identity stamped on every request's root span.
///
/// A projection of [`describe`]: names, versions and digests only. No URL, no
/// credential, no prompt text, no path. `agent.name` is OpenInference's; the
/// rest are namespaced under `agent.`. `service.version` is a resource
/// attribute, set once in [`super::init`].
pub fn trace_identity(record: &Value) -> Vec<(&'static str, String)> {
    let text = |key: &str| match &record[key] {
        Value::String(value) => value.clone(),
        Value::Null => String::new(),
        other => other.to_string(),
    };
    let mut identity = vec![
        ("agent.name", text("agent_name")),
        ("agent.revision.sha256", text("agent_revision_sha256")),
        ("agent.framework", text("agent_framework")),
        ("agent.framework.version", text("agent_framework_version")),
        ("agent.version", text("agent_version")),
        ("agent.build.commit", text("build_commit")),
        ("agent.config.sha256", text("config_sha256")),
        ("agent.prompt.template_sha256", text("prompt_sha256")),
        ("agent.prompt.rendered_sha256", text("rendered_prompt_sha256")),
        ("agent.tool_catalog.sha256", text("tool_catalog_sha256")),
        ("agent.guardrail_prompts.sha256", text("guardrails_prompts_sha256")),
        ("agent.behaviour_settings.sha256", text("behaviour_settings_sha256")),
        ("agent.model", text("model")),
        ("agent.guard_model", text("guard_model")),
        ("agent.llm.endpoint_sha256", text("endpoint_sha256")),
        ("agent.llm.provider", text("provider")),
        ("agent.reasoning_effort", text("reasoning_effort")),
        ("agent.guard_reasoning_effort", text("guard_reasoning_effort")),
    ];
    // An unset value (no provider name, an omitted reasoning effort) is left
    // off the span rather than exported as an empty string.
    identity.retain(|(_, value)| !value.is_empty());
    identity
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_endpoint_fingerprint_strips_credentials_and_cosmetics() {
        let plain = endpoint_sha256("http://host.docker.internal:11434/v1");
        assert_eq!(endpoint_sha256("HTTP://Host.Docker.Internal:11434/v1/"), plain);
        assert_eq!(endpoint_sha256("http://user:s3cret@host.docker.internal:11434/v1?key=abc#x"), plain);
        assert_ne!(endpoint_sha256("https://api.example.com/v1"), plain);
        assert_eq!(plain.len(), 64);
    }

    #[test]
    fn the_tool_catalogue_digest_ignores_discovery_order() {
        let a = ("a".to_string(), "first".to_string(), json!({"type": "object"}), "read_only");
        let b = ("b".to_string(), "second".to_string(), json!({"type": "object"}), "read_only");
        let forward = canonical_json(&tool_catalog([a.clone(), b.clone()]));
        let backward = canonical_json(&tool_catalog([b.clone(), a.clone()]));
        assert_eq!(forward, backward);
        let described_differently = ("b".to_string(), "changed".to_string(), json!({"type": "object"}), "read_only");
        assert_ne!(forward, canonical_json(&tool_catalog([a.clone(), described_differently])));
        let reclassified =
            ("b".to_string(), "second".to_string(), json!({"type": "object"}), "mutation_requiring_approval");
        assert_ne!(forward, canonical_json(&tool_catalog([a, reclassified])));
    }

    #[test]
    fn the_trace_identity_carries_no_url_or_empty_attribute() {
        let record = json!({
            "agent_name": "n", "agent_revision_sha256": "r", "model": "m", "provider": null,
            "reasoning_effort": "", "endpoint_sha256": "e", "config_sha256": null,
        });
        let identity = trace_identity(&record);
        assert!(identity.iter().all(|(_, value)| !value.contains("://")));
        assert!(!identity.iter().any(|(key, _)| {
            matches!(*key, "agent.llm.provider" | "agent.reasoning_effort" | "agent.config.sha256")
        }));
        assert!(identity.iter().any(|(key, value)| *key == "agent.name" && value == "n"));
    }
}
