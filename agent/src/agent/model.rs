//! Calling an LLM from Rust: an OpenAI-compatible chat model through Rig.
//!
//! Any OpenAI-compatible endpoint works, exactly as on `main`; the defaults
//! target a local Ollama. Rig's generic OpenAI dialect on the Chat
//! Completions route is used deliberately rather than its Ollama-specific
//! dialect, so pointing `LLM_BASE_URL` at a hosted provider changes nothing
//! else. Strict tool mode stays off: it would mark optional tool parameters
//! (`status`, `limit`) as required.

use rig_core::{
    completion::{AssistantContent, CompletionRequest},
    driver::Model,
    providers::openai::{OpenAIConfig, wire::Chat},
};
use serde_json::{Value, json};

use crate::config::ModelSettings;

/// One configured chat model.
pub type ChatModel = Model<Chat>;

pub fn chat_model(settings: &ModelSettings) -> ChatModel {
    OpenAIConfig::new(settings.api_key.clone())
        .with_base_url(settings.base_url.clone())
        .client()
        .chat(settings.model.clone())
}

/// Provider pass-through parameters. An empty `reasoning_effort` is *absent*,
/// not sent as `""` or `"null"`: a hosted provider that rejects the parameter
/// needs it missing. `none` is what suppresses Qwen3 thinking on Ollama.
pub fn additional_params(settings: &ModelSettings) -> Option<Value> {
    settings.reasoning_effort.as_ref().map(|effort| json!({ "reasoning_effort": effort }))
}

#[derive(Debug, thiserror::Error)]
#[error("the guard model call failed: {0}")]
pub struct GuardModelError(String);

/// The input rail's classifier call: the rendered self-check prompt as a
/// single user message, the prompt's token budget, NeMo's lowest
/// temperature (0.001). Returns the raw text; parsing is deterministic and
/// lives in `guardrails::input::classifier_says_safe`.
pub async fn classify(
    model: &ChatModel,
    settings: &ModelSettings,
    prompt: String,
    max_tokens: u64,
) -> Result<String, GuardModelError> {
    let request = CompletionRequest::new(prompt)
        .temperature(Some(0.001))
        .max_tokens(Some(max_tokens))
        .additional_params(additional_params(settings));
    let response = model.call(request).await.map_err(|error| GuardModelError(error.to_string()))?;
    Ok(response
        .choice
        .iter()
        .filter_map(|content| match content {
            AssistantContent::Text(text) => Some(text.text.as_str()),
            _ => None,
        })
        .collect::<Vec<_>>()
        .join(""))
}
