//! Everything one request's tools are bound to.
//!
//! A fresh Rig `Agent` is assembled per request (`agent::builder`), and every
//! tool it carries closes over one `Arc<RequestScope>`. That is how trusted
//! context reaches a tool without passing through the model: the caller's
//! identity, the request id and the event sink are fixed when the tools are
//! built, before the model has produced a single token, and no tool schema
//! has a field through which the model could replace them.

use std::sync::{Arc, Mutex};

use tokio::sync::mpsc;

use super::state::{ExecutionState, Transition};
use crate::{
    api::wire::{Step, StepFilter, WireEvent},
    approval::pending::{Decision, Prompt, WaitOutcome},
    identity::TrustedCaller,
    services::Services,
};

/// Ordered, back-pressured delivery of wire events to the client.
#[derive(Clone)]
pub struct EventSink {
    sender: mpsc::Sender<WireEvent>,
    filter: StepFilter,
}

impl EventSink {
    pub fn new(sender: mpsc::Sender<WireEvent>, filter: StepFilter) -> Self {
        Self { sender, filter }
    }

    /// Send one event. Steps the caller did not ask for are dropped here, in
    /// one place. A closed channel means the client went away; the run is
    /// being cancelled, so the error is ignored.
    pub async fn emit(&self, event: WireEvent) {
        if let Some(step_type) = event.step_type()
            && !self.filter.allows(step_type)
        {
            return;
        }
        let _ = self.sender.send(event).await;
    }

    pub async fn step(&self, step: Step) {
        self.emit(WireEvent::Step(step)).await;
    }
}

/// What asking a human produced.
#[derive(Debug, PartialEq, Eq)]
pub enum AskOutcome {
    Decided(Decision),
    TimedOut,
    /// The prompt could not even be opened (too many pending).
    Unavailable(String),
}

pub struct RequestScope {
    caller: TrustedCaller,
    execution_id: String,
    workflow_step_id: String,
    events: EventSink,
    services: Arc<Services>,
    state: Mutex<ExecutionState>,
}

impl RequestScope {
    pub fn new(caller: TrustedCaller, events: EventSink, services: Arc<Services>) -> Self {
        Self {
            caller,
            execution_id: uuid::Uuid::new_v4().to_string(),
            workflow_step_id: uuid::Uuid::new_v4().to_string(),
            events,
            services,
            state: Mutex::new(ExecutionState::Received),
        }
    }

    pub fn caller(&self) -> &TrustedCaller {
        &self.caller
    }

    pub fn execution_id(&self) -> &str {
        &self.execution_id
    }

    pub fn workflow_step_id(&self) -> &str {
        &self.workflow_step_id
    }

    pub fn events(&self) -> &EventSink {
        &self.events
    }

    pub fn services(&self) -> &Arc<Services> {
        &self.services
    }

    pub fn state(&self) -> ExecutionState {
        self.state.lock().unwrap_or_else(std::sync::PoisonError::into_inner).clone()
    }

    /// Apply a state transition. An illegal one is a bug, logged and refused
    /// rather than silently applied: the state machine is what the trace
    /// reports about how a run ended.
    pub fn transition(&self, transition: Transition) {
        let mut state = self.state.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
        match state.clone().apply(transition) {
            Ok(next) => {
                tracing::debug!(from = state.name(), to = next.name(), "execution state");
                *state = next;
            }
            Err(error) => tracing::error!(%error, "illegal execution state transition refused"),
        }
    }

    /// Suspend the run on a human prompt and wait for a verified decision.
    ///
    /// The interaction is opened *before* the event is sent, so a human who
    /// answers instantly finds it pending. The run stays suspended in this
    /// await; the client's SSE stream stays open, exactly as on `main`.
    pub async fn ask(&self, prompt: Prompt) -> AskOutcome {
        let registry = &self.services.interactions;
        let ticket = match registry.open(&self.execution_id, &self.caller, &prompt) {
            Ok(ticket) => ticket,
            Err(error) => return AskOutcome::Unavailable(format!("{error}. Nothing was changed.")),
        };
        let interaction_id = ticket.interaction_id().to_string();
        let span = tracing::info_span!("human_approval.wait", interaction.id = %interaction_id, interaction.kind = prompt.view.input_type, interaction.outcome = tracing::field::Empty);

        self.transition(Transition::AwaitApproval { interaction_id: interaction_id.clone() });
        self.events
            .emit(WireEvent::InteractionRequired {
                execution_id: self.execution_id.clone(),
                interaction_id,
                prompt: prompt.view,
            })
            .await;

        let timeout = self
            .services
            .settings
            .approval
            .as_ref()
            .map_or(std::time::Duration::from_secs(600), |settings| settings.interaction_timeout);
        let outcome = tracing::Instrument::instrument(ticket.wait(timeout), span.clone()).await;
        self.transition(Transition::Resume);
        match outcome {
            WaitOutcome::Decided(decision) => {
                let decision = decision.decision().clone();
                span.record(
                    "interaction.outcome",
                    match &decision {
                        Decision::Selected(_) => "selected",
                        Decision::Answered(_) => "answered",
                        Decision::Cancelled => "cancelled",
                    },
                );
                AskOutcome::Decided(decision)
            }
            WaitOutcome::TimedOut => {
                span.record("interaction.outcome", "timed_out");
                AskOutcome::TimedOut
            }
        }
    }
}
