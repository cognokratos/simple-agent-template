//! Typed configuration: secrets and endpoints from the environment, behaviour
//! from `config.yml`.
//!
//! Two rules shape everything here:
//!
//! * **Security-relevant settings fail fast.** A missing credential, a short
//!   approval secret, an unparseable boolean or a half-enabled approval feature
//!   stops the process at startup rather than resolving to a default.
//! * **Renamed variables keep a deprecated alias, never two meanings.** The
//!   names inherited from the retired NeMo Agent Toolkit runtime (`NAT_*`) are
//!   read only when their agent-neutral replacement is unset ([`RENAMED`]).

use std::{collections::BTreeMap, net::SocketAddr, path::PathBuf, time::Duration};

use serde::Deserialize;

/// Minimum approval-secret length. The MCP server enforces the same floor.
pub const MIN_APPROVAL_SECRET_CHARS: usize = 24;
/// Ceiling on an approval token's lifetime. The MCP verifier enforces its own
/// independent ceiling (`MAX_APPROVAL_LIFETIME_SECONDS`), because the minter is
/// not the trust boundary.
pub const MAX_TOKEN_TTL_SECONDS: u64 = 1_800;
pub const MIN_TOKEN_TTL_SECONDS: u64 = 60;

/// Why the service refused to start.
#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("{0} must be configured")]
    Missing(&'static str),
    #[error("{name} is invalid: {reason}")]
    Invalid { name: &'static str, reason: String },
    #[error("could not read {path}: {source}")]
    Read {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("could not parse {path}: {reason}")]
    Parse { path: PathBuf, reason: String },
}

fn invalid(name: &'static str, reason: impl Into<String>) -> ConfigError {
    ConfigError::Invalid { name, reason: reason.into() }
}

/// Source of environment values. A trait so tests can supply a map instead of
/// mutating the process environment, which is shared across test threads.
pub trait Env {
    fn get(&self, name: &str) -> Option<String>;
}

/// The real process environment.
pub struct ProcessEnv;

impl Env for ProcessEnv {
    fn get(&self, name: &str) -> Option<String> {
        std::env::var(name).ok()
    }
}

impl Env for BTreeMap<String, String> {
    fn get(&self, name: &str) -> Option<String> {
        BTreeMap::get(self, name).cloned()
    }
}

/// Environment variables renamed when the Rust agent replaced the NeMo Agent
/// Toolkit runtime, as `(current, deprecated)`. The deprecated name is still
/// read, with a startup warning, when the current one is unset or blank, so an
/// existing deployment keeps working for one deprecation cycle; the aliases are
/// then removed. Both set to *different* values stops startup: one of them is a
/// stale credential or setting that someone believes is in effect.
pub const RENAMED: &[(&str, &str)] = &[
    ("AGENT_API_KEY", "NAT_GATEWAY_API_KEY"),
    ("AGENT_TRACE_CAPTURE_CONTENT", "NAT_TRACE_CAPTURE_CONTENT"),
    ("AGENT_TRACE_CONTENT_MAX_CHARS", "NAT_TRACE_CONTENT_MAX_CHARS"),
];

/// An environment in which every [`RENAMED`] alias has been folded into its
/// current name, so the rest of the configuration reads one name per setting.
/// The deprecated names themselves read as unset.
struct Renamed<'a> {
    env: &'a dyn Env,
    resolved: BTreeMap<&'static str, String>,
    /// One deprecation notice per alias actually used.
    warnings: Vec<String>,
}

impl<'a> Renamed<'a> {
    fn new(env: &'a dyn Env) -> Result<Self, ConfigError> {
        let set = |name: &str| env.get(name).filter(|value| !value.trim().is_empty());
        let mut resolved = BTreeMap::new();
        let mut warnings = Vec::new();
        for &(current, deprecated) in RENAMED {
            match (set(current), set(deprecated)) {
                (Some(new), Some(old)) if new.trim() != old.trim() => {
                    return Err(invalid(
                        current,
                        format!("conflicts with the deprecated {deprecated}; unset {deprecated}"),
                    ));
                }
                (Some(value), _) => {
                    resolved.insert(current, value);
                }
                (None, Some(value)) => {
                    warnings.push(format!("{deprecated} is deprecated and will be removed; set {current} instead"));
                    resolved.insert(current, value);
                }
                (None, None) => {}
            }
        }
        Ok(Self { env, resolved, warnings })
    }
}

impl Env for Renamed<'_> {
    fn get(&self, name: &str) -> Option<String> {
        if let Some(value) = self.resolved.get(name) {
            return Some(value.clone());
        }
        if RENAMED.iter().any(|&(current, deprecated)| name == current || name == deprecated) {
            return None;
        }
        self.env.get(name)
    }
}

fn string_or(env: &dyn Env, name: &str, default: &str) -> String {
    match env.get(name) {
        Some(value) if !value.trim().is_empty() => value.trim().to_string(),
        _ => default.to_string(),
    }
}

/// A value whose *emptiness* is meaningful: `LLM_REASONING_EFFORT=""` means
/// "omit the parameter", which a hosted provider that rejects
/// `reasoning_effort` requires. Unset falls back to the default; set-but-empty
/// is preserved as `None`.
fn optional_param(env: &dyn Env, name: &str, default: Option<&str>) -> Option<String> {
    match env.get(name) {
        None => default.map(str::to_string),
        Some(value) if value.trim().is_empty() => None,
        Some(value) => Some(value.trim().to_string()),
    }
}

/// `LLM_PROVIDER`: optional, and constrained to a short identifier because it
/// is exported on every trace. It names the provider; it is not a URL.
fn provider_name(env: &dyn Env) -> Result<Option<String>, ConfigError> {
    let Some(value) = optional_param(env, "LLM_PROVIDER", None) else { return Ok(None) };
    let valid = value.len() <= 64
        && value.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, '-' | '_' | '.'));
    if !valid {
        return Err(invalid("LLM_PROVIDER", "use a short lowercase identifier such as `ollama` or `openai`"));
    }
    Ok(Some(value))
}

/// `<crate version>+<12-character commit>`, or `+unknown` for a build without one.
fn service_version(build_commit: &str) -> String {
    let commit: String = build_commit.chars().take(12).collect();
    format!("{}+{commit}", env!("CARGO_PKG_VERSION"))
}

fn required_secret(env: &dyn Env, name: &'static str) -> Result<String, ConfigError> {
    match env.get(name) {
        Some(value) if !value.trim().is_empty() => Ok(value.trim().to_string()),
        _ => Err(ConfigError::Missing(name)),
    }
}

/// Strict boolean. Unset or blank is the declared default; anything other than
/// the recognised spellings is a startup error, never a guess.
pub fn parse_bool(env: &dyn Env, name: &'static str, default: bool) -> Result<bool, ConfigError> {
    let Some(raw) = env.get(name) else { return Ok(default) };
    match raw.trim().to_ascii_lowercase().as_str() {
        "" => Ok(default),
        "1" | "true" | "yes" | "on" => Ok(true),
        "0" | "false" | "no" | "off" => Ok(false),
        _ => Err(invalid(name, format!("{raw:?} is not a recognised boolean"))),
    }
}

fn parse_usize(env: &dyn Env, name: &'static str, default: usize, floor: usize) -> Result<usize, ConfigError> {
    let Some(raw) = env.get(name) else { return Ok(default) };
    if raw.trim().is_empty() {
        return Ok(default);
    }
    let value: usize =
        raw.trim().parse().map_err(|_| invalid(name, format!("{raw:?} is not a non-negative integer")))?;
    Ok(value.max(floor))
}

/// Model endpoint settings. Any OpenAI-compatible endpoint; the defaults target
/// a local Ollama.
#[derive(Debug, Clone)]
pub struct ModelSettings {
    pub base_url: String,
    pub api_key: String,
    pub model: String,
    pub reasoning_effort: Option<String>,
    /// An optional, operator-chosen name for the provider (`LLM_PROVIDER`).
    /// Reported in provenance; the endpoint itself is reported only as a
    /// fingerprint (`telemetry::provenance::endpoint_sha256`).
    pub provider: Option<String>,
}

/// The approval feature, when — and only when — every switch agrees.
#[derive(Clone)]
pub struct ApprovalSettings {
    pub secret: Vec<u8>,
    pub token_ttl_seconds: u64,
    /// How long a pending interaction waits for its human before the run is
    /// cancelled, so an unanswered prompt cannot hold a run open indefinitely.
    pub interaction_timeout: Duration,
}

impl std::fmt::Debug for ApprovalSettings {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // The secret is never formatted, not even its length.
        f.debug_struct("ApprovalSettings")
            .field("secret", &"[redacted]")
            .field("token_ttl_seconds", &self.token_ttl_seconds)
            .field("interaction_timeout", &self.interaction_timeout)
            .finish()
    }
}

#[derive(Debug, Clone)]
pub struct GuardrailSettings {
    pub deterministic_fallback: bool,
    pub read_only_allow_override: bool,
    pub block_message: String,
    pub input_max_chars: usize,
    pub oversize_message: String,
    pub trace_capture_content: bool,
    pub trace_capture_raw_output: bool,
    pub trace_max_chars: usize,
    /// Ceiling on one streamed answer. PII masking runs in a bounded window,
    /// so this bounds the answer itself.
    pub max_answer_chars: usize,
}

#[derive(Debug, Clone)]
pub struct TelemetrySettings {
    pub traces_endpoint: Option<String>,
    pub service_name: String,
    pub deployment_environment: String,
    pub capture_content: bool,
    pub content_max_chars: usize,
    pub export_user_id: bool,
    /// Rig's own `gen_ai.prompt`/`gen_ai.completion` span content. Off by
    /// default: model output on those spans is *pre-output-rail*, so it can
    /// carry exactly the credential the output rail exists to withhold.
    pub capture_model_content: bool,
    /// OpenTelemetry `service.version`: the crate version plus the build commit.
    pub service_version: String,
}

/// Everything the service needs, resolved once at startup.
#[derive(Debug, Clone)]
pub struct Settings {
    pub bind_address: SocketAddr,
    pub gateway_api_key: String,
    pub agent: ModelSettings,
    pub guard: ModelSettings,
    pub mcp_url: String,
    pub mcp_api_key: String,
    pub approval: Option<ApprovalSettings>,
    pub guardrails: GuardrailSettings,
    pub telemetry: TelemetrySettings,
    pub build_commit: String,
    pub config_path: PathBuf,
    pub file: AgentConfigFile,
    /// Non-fatal configuration notices (deprecated names, settings without
    /// effect). Collected rather than logged, because configuration is
    /// resolved before the tracing subscriber exists: `main` prints them to
    /// stderr, where a log at that point would be silently dropped.
    pub warnings: Vec<String>,
}

impl Settings {
    /// Resolve and validate everything. Any error here stops the process.
    pub fn load(env: &dyn Env) -> Result<Self, ConfigError> {
        let config_path = PathBuf::from(string_or(env, "AGENT_CONFIG_PATH", "/app/config.yml"));
        let raw = std::fs::read_to_string(&config_path)
            .map_err(|source| ConfigError::Read { path: config_path.clone(), source })?;
        let file =
            AgentConfigFile::parse(&raw).map_err(|reason| ConfigError::Parse { path: config_path.clone(), reason })?;
        Self::resolve(env, config_path, file)
    }

    pub fn resolve(env: &dyn Env, config_path: PathBuf, file: AgentConfigFile) -> Result<Self, ConfigError> {
        let renamed = Renamed::new(env)?;
        let mut warnings = renamed.warnings.clone();
        let env = &renamed;
        let bind_address = string_or(env, "AGENT_BIND_ADDRESS", "0.0.0.0:8000")
            .parse()
            .map_err(|_| invalid("AGENT_BIND_ADDRESS", "not a socket address"))?;

        let base_url = string_or(env, "LLM_BASE_URL", "http://host.docker.internal:11434/v1");
        let api_key = string_or(env, "LLM_API_KEY", "ollama");
        let provider = provider_name(env)?;
        let agent = ModelSettings {
            base_url: base_url.clone(),
            api_key: api_key.clone(),
            model: string_or(env, "LLM_MODEL", "qwen3:8b"),
            reasoning_effort: optional_param(env, "LLM_REASONING_EFFORT", None),
            provider: provider.clone(),
        };
        // Deliberately does not inherit LLM_MODEL: the fallback ends at the
        // literal qwen3:8b.
        let guard = ModelSettings {
            base_url,
            api_key,
            model: string_or(env, "LLM_GUARD_MODEL", "qwen3:8b"),
            reasoning_effort: optional_param(env, "LLM_GUARD_REASONING_EFFORT", None),
            provider,
        };

        let approval = resolve_approval(env, &file)?;
        if !parse_bool(env, "HITL_STRICT_INTERACTION_OWNERSHIP", true)? {
            // Accepted for configuration parity only. Every interaction this
            // service creates has an owner by construction, so there is no
            // "unowned" case for a non-strict mode to allow through.
            warnings.push(
                "HITL_STRICT_INTERACTION_OWNERSHIP=false has no effect: interaction ownership is always \
                 enforced by this agent"
                    .to_string(),
            );
        }

        let guardrails = GuardrailSettings {
            deterministic_fallback: parse_bool(env, "GUARDRAILS_INPUT_DETERMINISTIC_FALLBACK", true)?,
            read_only_allow_override: parse_bool(env, "GUARDRAILS_INPUT_READ_ONLY_ALLOW_OVERRIDE", true)?,
            block_message: string_or(
                env,
                "GUARDRAILS_INPUT_BLOCK_MESSAGE",
                "I'm sorry, I can't help with that request.",
            ),
            input_max_chars: parse_usize(env, "GUARDRAILS_INPUT_MAX_CHARS", 32_000, 256)?,
            oversize_message: string_or(
                env,
                "GUARDRAILS_INPUT_OVERSIZE_MESSAGE",
                "That message is too long for me to review safely. Please shorten it and try again.",
            ),
            trace_capture_content: parse_bool(env, "GUARDRAILS_TRACE_CAPTURE_CONTENT", true)?,
            trace_capture_raw_output: parse_bool(env, "GUARDRAILS_TRACE_CAPTURE_RAW_OUTPUT", false)?,
            trace_max_chars: parse_usize(env, "GUARDRAILS_TRACE_MAX_CHARS", 16_384, 256)?,
            max_answer_chars: parse_usize(env, "GUARDRAILS_PII_MAX_BUFFER_CHARS", 200_000, 1_024)?,
        };

        let traces_endpoint = env
            .get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
            .or_else(|| env.get("OTEL_COLLECTOR_TRACES_ENDPOINT"))
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty());
        let telemetry = TelemetrySettings {
            traces_endpoint,
            service_name: string_or(env, "OTEL_SERVICE_NAME", "tickets-agent"),
            deployment_environment: string_or(env, "DEPLOYMENT_ENVIRONMENT", "local"),
            capture_content: parse_bool(env, "AGENT_TRACE_CAPTURE_CONTENT", true)?,
            content_max_chars: parse_usize(env, "AGENT_TRACE_CONTENT_MAX_CHARS", 65_536, 256)?,
            export_user_id: parse_bool(env, "OTEL_TRACE_USER_ID", false)?,
            capture_model_content: parse_bool(env, "AGENT_TRACE_CAPTURE_MODEL_CONTENT", false)?,
            service_version: service_version(&string_or(env, "AGENT_BUILD_COMMIT", "unknown")),
        };

        Ok(Self {
            bind_address,
            gateway_api_key: required_secret(env, "AGENT_API_KEY")?,
            agent,
            guard,
            mcp_url: string_or(env, "TICKETS_MCP_URL", "http://mcp-server:8080/mcp"),
            mcp_api_key: required_secret(env, "MCP_API_KEY")?,
            approval,
            guardrails,
            telemetry,
            build_commit: string_or(env, "AGENT_BUILD_COMMIT", "unknown"),
            config_path,
            file,
            warnings,
        })
    }
}

/// The approval feature is on only when all three switches agree: the tool is declared in `config.yml`, the secret is set, and the
/// interaction endpoints are enabled. A *partial* configuration is an error,
/// not a quiet fallback, because it means someone believes approvals are on.
/// The one exception is a secret with nothing else: that is the MCP server's
/// switch too, and a read-only agent beside it is a valid deployment.
fn resolve_approval(env: &dyn Env, file: &AgentConfigFile) -> Result<Option<ApprovalSettings>, ConfigError> {
    let declared = file.tools.approval.as_ref();
    let interactive = parse_bool(env, "HITL_ENABLE_INTERACTIVE", false)?;
    let secret = env.get("HITL_APPROVAL_SECRET").filter(|value| !value.trim().is_empty());

    if let Some(secret) = &secret
        && secret.chars().count() < MIN_APPROVAL_SECRET_CHARS
    {
        return Err(invalid(
            "HITL_APPROVAL_SECRET",
            format!("must contain at least {MIN_APPROVAL_SECRET_CHARS} characters"),
        ));
    }

    match (declared, secret, interactive) {
        (None, _, false) => Ok(None),
        (None, _, true) => {
            Err(invalid("HITL_ENABLE_INTERACTIVE", "is true but config.yml declares no approval tool (tools.approval)"))
        }
        (Some(_), None, _) => {
            Err(invalid("HITL_APPROVAL_SECRET", "config.yml declares an approval tool but no approval secret is set"))
        }
        (Some(_), Some(_), false) => Err(invalid(
            "HITL_ENABLE_INTERACTIVE",
            "config.yml declares an approval tool but interaction endpoints are disabled",
        )),
        (Some(tool), Some(secret), true) => {
            let ttl = tool.ticket_priority_change.token_ttl_seconds;
            if !(MIN_TOKEN_TTL_SECONDS..=MAX_TOKEN_TTL_SECONDS).contains(&ttl) {
                return Err(invalid(
                    "tools.approval.ticket_priority_change.token_ttl_seconds",
                    format!("must be between {MIN_TOKEN_TTL_SECONDS} and {MAX_TOKEN_TTL_SECONDS}"),
                ));
            }
            let timeout = parse_usize(env, "HITL_INTERACTION_TIMEOUT_SECONDS", 600, 5)?;
            Ok(Some(ApprovalSettings {
                secret: secret.into_bytes(),
                token_ttl_seconds: ttl,
                interaction_timeout: Duration::from_secs(timeout as u64),
            }))
        }
    }
}

// ---------------------------------------------------------------------------
// config.yml
// ---------------------------------------------------------------------------

/// The behavioural configuration file. Unknown keys are refused: a typo in a
/// policy file must not silently drop the policy.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentConfigFile {
    pub workflow: WorkflowConfig,
    pub tools: ToolsConfig,
    pub guardrails: GuardrailsConfig,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkflowConfig {
    pub name: String,
    pub max_tool_calls: usize,
    pub max_history: usize,
    pub temperature: f64,
    pub max_tokens: u64,
    pub request_timeout_seconds: u64,
    pub system_prompt: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolsConfig {
    pub mcp: McpToolsConfig,
    #[serde(default)]
    pub approval: Option<ApprovalToolsConfig>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct McpToolsConfig {
    /// Span and step-name prefix, `<group>__<tool>` (`tickets_mcp`). Trace
    /// queries and evaluation scorers match on it.
    pub function_group: String,
    pub tool_call_timeout_seconds: u64,
    /// The allow-list. A tool the server offers that is not listed here is
    /// never advertised to the model.
    pub include: Vec<String>,
    #[serde(default)]
    pub overrides: BTreeMap<String, ToolOverride>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolOverride {
    pub description: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ApprovalToolsConfig {
    pub ticket_priority_change: ApprovalToolConfig,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ApprovalToolConfig {
    pub token_ttl_seconds: u64,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GuardrailsConfig {
    pub prompts: Vec<RailPrompt>,
    pub output: OutputRailConfig,
}

#[derive(Debug, Clone, serde::Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RailPrompt {
    pub task: String,
    pub content: String,
    pub max_tokens: u64,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OutputRailConfig {
    pub secret_patterns: Vec<String>,
    pub pii_entities: Vec<String>,
}

impl AgentConfigFile {
    pub fn parse(raw: &str) -> Result<Self, String> {
        let file: Self = serde_saphyr::from_str(raw).map_err(|error| error.to_string())?;
        file.validate()?;
        Ok(file)
    }

    fn validate(&self) -> Result<(), String> {
        let workflow = &self.workflow;
        if workflow.max_tool_calls == 0 || workflow.max_tool_calls > 100 {
            return Err("workflow.max_tool_calls must be between 1 and 100".into());
        }
        if workflow.max_history == 0 {
            return Err("workflow.max_history must be at least 1".into());
        }
        if workflow.system_prompt.trim().is_empty() {
            return Err("workflow.system_prompt must not be empty".into());
        }
        if self.tools.mcp.include.is_empty() {
            return Err("tools.mcp.include must name at least one tool".into());
        }
        for name in self.tools.mcp.overrides.keys() {
            if !self.tools.mcp.include.contains(name) {
                return Err(format!("tools.mcp.overrides names {name:?}, which is not included"));
            }
        }
        if self.self_check_prompt().is_none() {
            return Err("guardrails.prompts must contain the self_check_input task".into());
        }
        if self.guardrails.output.secret_patterns.is_empty() {
            return Err("guardrails.output.secret_patterns must not be empty".into());
        }
        Ok(())
    }

    pub fn self_check_prompt(&self) -> Option<&RailPrompt> {
        self.guardrails.prompts.iter().find(|prompt| prompt.task == "self_check_input")
    }

    /// Every tool name the configuration can expose to the model.
    pub fn exposed_tools(&self, approvals_enabled: bool) -> Vec<String> {
        let mut names = self.tools.mcp.include.clone();
        if approvals_enabled && self.tools.approval.is_some() {
            names.push(crate::approval::TOOL_NAME.to_string());
        }
        names.sort();
        names
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    pub(crate) const SAMPLE: &str = include_str!("../config.yml");

    fn env(pairs: &[(&str, &str)]) -> BTreeMap<String, String> {
        let mut map: BTreeMap<String, String> = [("AGENT_API_KEY", "gateway-key"), ("MCP_API_KEY", "mcp-key")]
            .into_iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        for (key, value) in pairs {
            map.insert((*key).to_string(), (*value).to_string());
        }
        map
    }

    fn file() -> AgentConfigFile {
        AgentConfigFile::parse(SAMPLE).expect("shipped config.yml parses")
    }

    fn with_approval_tool() -> AgentConfigFile {
        let mut file = file();
        file.tools.approval =
            Some(ApprovalToolsConfig { ticket_priority_change: ApprovalToolConfig { token_ttl_seconds: 600 } });
        file
    }

    #[test]
    fn the_shipped_configuration_is_read_only() {
        let settings = Settings::resolve(&env(&[]), "config.yml".into(), file()).unwrap();
        assert!(settings.approval.is_none());
        assert_eq!(settings.file.exposed_tools(false), vec!["get_ticket", "search_tickets"]);
    }

    #[test]
    fn credentials_are_required() {
        let mut missing = env(&[]);
        missing.remove("AGENT_API_KEY");
        assert!(matches!(Settings::resolve(&missing, "c".into(), file()), Err(ConfigError::Missing("AGENT_API_KEY"))));
        let blank = env(&[("MCP_API_KEY", "   ")]);
        assert!(matches!(Settings::resolve(&blank, "c".into(), file()), Err(ConfigError::Missing("MCP_API_KEY"))));
    }

    #[test]
    fn deprecated_nat_names_are_read_only_when_the_current_name_is_unset() {
        let mut legacy = env(&[
            ("NAT_GATEWAY_API_KEY", "legacy-key"),
            ("NAT_TRACE_CAPTURE_CONTENT", "false"),
            ("NAT_TRACE_CONTENT_MAX_CHARS", "1000"),
        ]);
        legacy.remove("AGENT_API_KEY");
        let settings = Settings::resolve(&legacy, "c".into(), file()).unwrap();
        assert_eq!(settings.gateway_api_key, "legacy-key");
        assert_eq!(settings.warnings.len(), 3, "{:?}", settings.warnings);
        assert!(
            settings.warnings[0].contains("NAT_GATEWAY_API_KEY is deprecated")
                && settings.warnings[0].contains("AGENT_API_KEY")
        );
        assert!(!settings.telemetry.capture_content);
        assert_eq!(settings.telemetry.content_max_chars, 1000);

        // The current name wins; an identical deprecated value is harmless.
        let both = env(&[("NAT_GATEWAY_API_KEY", "gateway-key"), ("AGENT_TRACE_CAPTURE_CONTENT", "false")]);
        let settings = Settings::resolve(&both, "c".into(), file()).unwrap();
        assert_eq!(settings.gateway_api_key, "gateway-key");
        assert!(!settings.telemetry.capture_content);
        assert!(settings.warnings.is_empty(), "an alias that was not used is not reported: {:?}", settings.warnings);
    }

    #[test]
    fn non_fatal_notices_are_collected_not_logged_before_telemetry_exists() {
        assert!(Settings::resolve(&env(&[]), "c".into(), file()).unwrap().warnings.is_empty());
        let lax =
            Settings::resolve(&env(&[("HITL_STRICT_INTERACTION_OWNERSHIP", "false")]), "c".into(), file()).unwrap();
        assert_eq!(lax.warnings.len(), 1);
        assert!(lax.warnings[0].starts_with("HITL_STRICT_INTERACTION_OWNERSHIP=false has no effect"));
    }

    #[test]
    fn a_deprecated_name_that_disagrees_with_the_current_one_stops_startup() {
        for (current, deprecated, a, b) in [
            ("AGENT_API_KEY", "NAT_GATEWAY_API_KEY", "gateway-key", "other-key"),
            ("AGENT_TRACE_CAPTURE_CONTENT", "NAT_TRACE_CAPTURE_CONTENT", "true", "false"),
            ("AGENT_TRACE_CONTENT_MAX_CHARS", "NAT_TRACE_CONTENT_MAX_CHARS", "1000", "2000"),
        ] {
            let result = Settings::resolve(&env(&[(current, a), (deprecated, b)]), "c".into(), file());
            assert!(
                matches!(result, Err(ConfigError::Invalid { name, .. }) if name == current),
                "{current} vs {deprecated}"
            );
        }
    }

    #[test]
    fn an_unrecognised_security_boolean_stops_startup() {
        for name in [
            "GUARDRAILS_INPUT_DETERMINISTIC_FALLBACK",
            "GUARDRAILS_TRACE_CAPTURE_RAW_OUTPUT",
            "OTEL_TRACE_USER_ID",
            "HITL_ENABLE_INTERACTIVE",
        ] {
            let result = Settings::resolve(&env(&[(name, "Ture")]), "c".into(), file());
            assert!(matches!(result, Err(ConfigError::Invalid { .. })), "{name}");
        }
    }

    #[test]
    fn approvals_need_every_switch() {
        let secret = "s".repeat(MIN_APPROVAL_SECRET_CHARS);
        // Secret alone: valid read-only agent beside an approval-capable MCP.
        let alone = Settings::resolve(&env(&[("HITL_APPROVAL_SECRET", &secret)]), "c".into(), file());
        assert!(alone.unwrap().approval.is_none());
        // Interactive without a declared tool.
        assert!(Settings::resolve(&env(&[("HITL_ENABLE_INTERACTIVE", "true")]), "c".into(), file()).is_err());
        // Declared tool without a secret.
        assert!(
            Settings::resolve(&env(&[("HITL_ENABLE_INTERACTIVE", "true")]), "c".into(), with_approval_tool()).is_err()
        );
        // Declared tool and secret without interaction endpoints.
        assert!(
            Settings::resolve(&env(&[("HITL_APPROVAL_SECRET", &secret)]), "c".into(), with_approval_tool()).is_err()
        );
        // All three.
        let all = Settings::resolve(
            &env(&[("HITL_APPROVAL_SECRET", &secret), ("HITL_ENABLE_INTERACTIVE", "true")]),
            "c".into(),
            with_approval_tool(),
        )
        .unwrap();
        assert!(all.approval.is_some());
        assert_eq!(all.file.exposed_tools(true), vec!["get_ticket", "search_tickets", "ticket_priority_change"]);
    }

    #[test]
    fn a_short_approval_secret_is_refused() {
        let short = "s".repeat(MIN_APPROVAL_SECRET_CHARS - 1);
        let result = Settings::resolve(
            &env(&[("HITL_APPROVAL_SECRET", &short), ("HITL_ENABLE_INTERACTIVE", "true")]),
            "c".into(),
            with_approval_tool(),
        );
        assert!(matches!(result, Err(ConfigError::Invalid { name: "HITL_APPROVAL_SECRET", .. })));
    }

    #[test]
    fn a_token_ttl_outside_the_ceiling_is_refused() {
        let secret = "s".repeat(MIN_APPROVAL_SECRET_CHARS);
        let mut file = with_approval_tool();
        file.tools.approval.as_mut().unwrap().ticket_priority_change.token_ttl_seconds = 3_600;
        let result = Settings::resolve(
            &env(&[("HITL_APPROVAL_SECRET", &secret), ("HITL_ENABLE_INTERACTIVE", "true")]),
            "c".into(),
            file,
        );
        assert!(result.is_err());
    }

    #[test]
    fn an_empty_reasoning_effort_is_omitted_not_sent() {
        let settings = Settings::resolve(&env(&[("LLM_REASONING_EFFORT", "")]), "c".into(), file()).unwrap();
        assert_eq!(settings.agent.reasoning_effort, None);
        let settings = Settings::resolve(&env(&[("LLM_REASONING_EFFORT", "none")]), "c".into(), file()).unwrap();
        assert_eq!(settings.agent.reasoning_effort.as_deref(), Some("none"));
    }

    #[test]
    fn unknown_config_keys_are_refused() {
        let typo = SAMPLE.replace("max_tool_calls:", "max_tool_call:");
        assert!(AgentConfigFile::parse(&typo).is_err());
    }

    #[test]
    fn the_input_limit_has_a_floor() {
        let settings = Settings::resolve(&env(&[("GUARDRAILS_INPUT_MAX_CHARS", "10")]), "c".into(), file()).unwrap();
        assert_eq!(settings.guardrails.input_max_chars, 256);
    }

    #[test]
    fn the_approval_secret_never_formats() {
        let settings = ApprovalSettings {
            secret: b"super-secret-value-123456".to_vec(),
            token_ttl_seconds: 600,
            interaction_timeout: Duration::from_secs(600),
        };
        assert!(!format!("{settings:?}").contains("super-secret"));
    }
}
