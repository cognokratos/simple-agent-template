//! The SSE vocabulary of `POST /v1/workflow/full`.
//!
//! This is the agent-service contract as its two consumers actually parse it
//! (`ui/app/api/gateway/chat/route.ts` and `evaluation/client.py`), and it is the
//! same vocabulary NAT's `/v1/workflow/full` emits on `main`:
//!
//! ```text
//! intermediate_data: {"id","parent_id","type","name","payload"}   a step
//! data: {"value":"<answer fragment>"}                              answer text
//! event: interaction_required\ndata: {...}                         human approval
//! {"error":"...","message":"..."}                                  workflow error
//! ```
//!
//! The last one is not a well-formed SSE field. It is what NAT emits for a
//! workflow error, and both consumers detect it by its leading `{`, so it is
//! reproduced exactly rather than "fixed" into a form neither would read.
//!
//! Every event is a typed enum variant and serialisation lives in one place,
//! so a field the consumers depend on cannot drift in one call site only.

use std::collections::BTreeSet;

use serde::Serialize;
use serde_json::{Value, json};

/// Step types, as NAT names them. The consumers match on these strings.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum StepType {
    WorkflowStart,
    WorkflowEnd,
    ToolStart,
    ToolEnd,
    FunctionStart,
    FunctionEnd,
}

impl StepType {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::WorkflowStart => "WORKFLOW_START",
            Self::WorkflowEnd => "WORKFLOW_END",
            Self::ToolStart => "TOOL_START",
            Self::ToolEnd => "TOOL_END",
            Self::FunctionStart => "FUNCTION_START",
            Self::FunctionEnd => "FUNCTION_END",
        }
    }

    fn parse(value: &str) -> Option<Self> {
        Some(match value.trim().to_ascii_uppercase().as_str() {
            "WORKFLOW_START" => Self::WorkflowStart,
            "WORKFLOW_END" => Self::WorkflowEnd,
            "TOOL_START" => Self::ToolStart,
            "TOOL_END" => Self::ToolEnd,
            "FUNCTION_START" => Self::FunctionStart,
            "FUNCTION_END" => Self::FunctionEnd,
            _ => return None,
        })
    }
}

/// The `filter_steps` query parameter: which step types the caller wants.
/// Absent means all of them, as with NAT. Unknown names are ignored, so a
/// caller asking for a NAT step type this service does not emit (`LLM_START`,
/// say) simply receives none.
#[derive(Debug, Clone)]
pub struct StepFilter(Option<BTreeSet<StepType>>);

impl StepFilter {
    pub fn all() -> Self {
        Self(None)
    }

    pub fn parse(raw: Option<&str>) -> Self {
        match raw {
            None => Self(None),
            Some(raw) => Self(Some(raw.split(',').filter_map(StepType::parse).collect())),
        }
    }

    pub fn allows(&self, step: StepType) -> bool {
        self.0.as_ref().is_none_or(|set| set.contains(&step))
    }
}

/// One intermediate step. `payload` follows the shape NAT's
/// `IntermediateStepPayload` gives the fields the consumers read:
/// `data.input`, `data.output`, `metadata.tool_inputs`, `metadata.tool_outputs`.
#[derive(Debug, Clone, Serialize)]
pub struct Step {
    pub id: String,
    pub parent_id: Option<String>,
    #[serde(rename = "type")]
    pub step_type: StepType,
    pub name: String,
    pub payload: Value,
}

impl Step {
    pub fn new(step_type: StepType, id: impl Into<String>, name: impl Into<String>) -> Self {
        Self { id: id.into(), parent_id: None, step_type, name: name.into(), payload: json!({}) }
    }

    pub fn parent(mut self, parent: impl Into<String>) -> Self {
        self.parent_id = Some(parent.into());
        self
    }

    /// A tool or function start: arguments under both places the consumers look.
    pub fn with_input(mut self, input: Value) -> Self {
        self.payload = json!({
            "event_type": self.step_type.as_str(),
            "name": self.name,
            "UUID": self.id,
            "data": { "input": input.clone() },
            "metadata": { "tool_inputs": input },
        });
        self
    }

    /// A tool or function end: the result the evaluator grounds answers against.
    pub fn with_output(mut self, input: Value, output: Value) -> Self {
        self.payload = json!({
            "event_type": self.step_type.as_str(),
            "name": self.name,
            "UUID": self.id,
            "data": { "input": input.clone(), "output": output.clone() },
            "metadata": { "tool_inputs": input, "tool_outputs": output },
        });
        self
    }

    /// Workflow boundaries carry trace correlation, which the evaluator reads
    /// to link a result to its MLflow trace.
    pub fn with_metadata(mut self, metadata: Value) -> Self {
        self.payload = json!({
            "event_type": self.step_type.as_str(),
            "name": self.name,
            "UUID": self.id,
            "metadata": { "provided_metadata": metadata },
        });
        self
    }
}

/// What the human is asked. The consumers render `input_type` `radio` as one
/// button per option and `text` as a reason box (`ui/app/page.tsx`).
#[derive(Debug, Clone, Serialize)]
pub struct PromptView {
    pub input_type: &'static str,
    pub text: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub placeholder: Option<String>,
    pub required: bool,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub options: Vec<OptionView>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct OptionView {
    pub id: String,
    pub label: String,
    pub value: String,
    pub description: String,
}

/// One server-sent event.
#[derive(Debug, Clone)]
pub enum WireEvent {
    Step(Step),
    /// A fragment of the *released* answer: only text that has passed the
    /// output rail is ever wrapped in this variant.
    Answer(String),
    InteractionRequired {
        execution_id: String,
        interaction_id: String,
        prompt: PromptView,
    },
    Error {
        message: String,
    },
}

impl WireEvent {
    pub fn step_type(&self) -> Option<StepType> {
        match self {
            Self::Step(step) => Some(step.step_type),
            _ => None,
        }
    }

    /// The exact bytes on the wire, including the blank-line terminator.
    pub fn to_sse(&self) -> String {
        match self {
            Self::Step(step) => format!("intermediate_data: {}\n\n", to_json(step)),
            Self::Answer(text) => format!("data: {}\n\n", json!({ "value": text })),
            Self::InteractionRequired { execution_id, interaction_id, prompt } => format!(
                "event: interaction_required\ndata: {}\n\n",
                json!({
                    "execution_id": execution_id,
                    "interaction_id": interaction_id,
                    "prompt": prompt,
                })
            ),
            Self::Error { message } => {
                format!("{}\n\n", json!({ "error": "workflow_error", "message": message }))
            }
        }
    }
}

fn to_json<T: Serialize>(value: &T) -> String {
    // Serialising these types cannot fail: every field is a string, a bool or
    // a `serde_json::Value`. The fallback keeps the stream well-formed anyway.
    serde_json::to_string(value).unwrap_or_else(|_| "{}".to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn answer_text_is_a_json_string_even_when_it_looks_like_a_number() {
        // `100`, `true` and dates must stay strings: the UI and the evaluator
        // only parse JSON *containers* inside `value` (ui/lib/nat-wire.ts).
        for text in ["100", "true", "20260920", "null"] {
            let sse = WireEvent::Answer(text.to_string()).to_sse();
            assert_eq!(sse, format!("data: {{\"value\":\"{text}\"}}\n\n"));
        }
    }

    #[test]
    fn a_tool_step_carries_its_input_where_both_consumers_read_it() {
        let step = Step::new(StepType::ToolStart, "t1", "search_tickets").with_input(json!({"status": "open"}));
        let sse = WireEvent::Step(step).to_sse();
        let body: Value = serde_json::from_str(sse.trim_start_matches("intermediate_data: ").trim()).unwrap();
        assert_eq!(body["type"], "TOOL_START");
        assert_eq!(body["payload"]["data"]["input"]["status"], "open");
        assert_eq!(body["payload"]["metadata"]["tool_inputs"]["status"], "open");
    }

    #[test]
    fn an_interaction_event_is_announced_by_an_event_line() {
        let event = WireEvent::InteractionRequired {
            execution_id: "e".into(),
            interaction_id: "i".into(),
            prompt: PromptView {
                input_type: "text",
                text: "Why?".into(),
                placeholder: None,
                required: true,
                options: vec![],
            },
        };
        let sse = event.to_sse();
        assert!(sse.starts_with("event: interaction_required\ndata: {"));
        assert!(sse.contains("\"execution_id\":\"e\""));
    }

    #[test]
    fn a_workflow_error_is_a_bare_json_block_as_on_main() {
        let sse = WireEvent::Error { message: "boom".into() }.to_sse();
        assert!(sse.starts_with('{'));
        let body: Value = serde_json::from_str(sse.trim()).unwrap();
        assert_eq!(body["message"], "boom");
    }

    #[test]
    fn step_filters_follow_nat_semantics() {
        let all = StepFilter::parse(None);
        assert!(all.allows(StepType::WorkflowStart));
        let gateway = StepFilter::parse(Some("TOOL_START,TOOL_END,FUNCTION_START,FUNCTION_END"));
        assert!(gateway.allows(StepType::ToolStart));
        assert!(!gateway.allows(StepType::WorkflowStart));
        let unknown = StepFilter::parse(Some("LLM_START"));
        assert!(!unknown.allows(StepType::ToolStart));
    }
}
