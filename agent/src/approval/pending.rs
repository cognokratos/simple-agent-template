//! Pending human interactions: the suspension point of an agent run.
//!
//! When the approval gate needs a human, it *opens* an interaction here and
//! awaits its [`PendingTicket`]. The run is suspended inside that await — the
//! same shape as NAT's paused coroutine on `main` — and the SSE stream stays
//! open. The only way to resume it with a decision is
//! [`InteractionRegistry::respond`], called by the authenticated response
//! route, which checks, in order:
//!
//! 1. **the interaction exists** and is still pending (single use);
//! 2. **the responder owns it** — the authenticated user the prompt was
//!    addressed to, not merely someone who knows two UUIDs;
//! 3. **the response is the kind of answer this prompt asked for** (a radio
//!    answer to a text prompt is never valid, whatever it contains);
//! 4. **the choice was offered, as a pair** — the submitted `(id, value)` must
//!    be one option this prompt offered, not an id from one option and a value
//!    from another; a cancellation must be self-consistent.
//!
//! Only after all four does it construct a [`VerifiedDecision`] — a type with
//! no public constructor — and hand it to the suspended run. A rejected
//! response leaves the interaction pending, so the legitimate owner can still
//! answer it.
//!
//! State is in memory, like NAT's execution store on `main`. The run itself is
//! a live task holding the client's stream, so durable storage of the pending
//! record alone would not let anything resume after a restart; see
//! docs/LIMITATIONS.md for what durable suspension would require.

use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
    time::Duration,
};

use serde::Deserialize;
use tokio::sync::oneshot;

use crate::{
    api::wire::{OptionView, PromptView},
    identity::TrustedCaller,
};

/// Cancellation is part of the interaction protocol, shared verbatim with the
/// gateway (`proxy.rs`) and the UI (`page.tsx`).
pub const CANCEL_SENTINEL: &str = "__CANCEL__";
pub const CANCEL_ID: &str = "cancel";

/// Bound on simultaneously pending interactions across all users.
pub const MAX_PENDING: usize = 1_024;

/// What one prompt asks. The view is what the human sees; the offer is what
/// is checked when they answer, derived from the same data so the two cannot
/// disagree.
#[derive(Debug, Clone)]
pub struct Prompt {
    pub view: PromptView,
    offer: Offer,
}

#[derive(Debug, Clone, PartialEq)]
enum Offer {
    /// One of these options, matched on `(id, value)` together.
    Choice(Vec<OptionView>),
    /// Free text (a rationale); cancellation is the sentinel text.
    Text,
}

impl Prompt {
    /// A choice among options. A cancel option is always appended.
    pub fn choice(text: String, mut options: Vec<OptionView>) -> Self {
        options.push(OptionView {
            id: CANCEL_ID.into(),
            label: "Cancel".into(),
            value: CANCEL_SENTINEL.into(),
            description: "Change nothing.".into(),
        });
        Self {
            view: PromptView { input_type: "radio", text, placeholder: None, required: true, options: options.clone() },
            offer: Offer::Choice(options),
        }
    }

    pub fn text(text: String, placeholder: String) -> Self {
        Self {
            view: PromptView {
                input_type: "text",
                text,
                placeholder: Some(placeholder),
                required: true,
                options: vec![],
            },
            offer: Offer::Text,
        }
    }
}

/// A response as it arrives on the wire: untrusted until [`InteractionRegistry::respond`]
/// has checked it. Field names follow the gateway's `InteractionResponsePayload`.
#[derive(Debug, Clone, Deserialize, PartialEq)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum HumanResponse {
    Text { text: String },
    BinaryChoice { selected_option: BinaryOption },
    Radio { selected_option: RadioOption },
}

#[derive(Debug, Clone, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct BinaryOption {
    pub id: String,
    pub label: String,
    pub value: bool,
}

#[derive(Debug, Clone, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RadioOption {
    pub id: String,
    pub label: String,
    pub value: String,
    #[serde(default)]
    pub description: String,
}

impl HumanResponse {
    fn kind(&self) -> &'static str {
        match self {
            Self::Text { .. } => "text",
            Self::BinaryChoice { .. } => "binary_choice",
            Self::Radio { .. } => "radio",
        }
    }
}

/// What the human decided, after every check. No public constructor: the
/// approval gate can only ever obtain one from a [`PendingTicket`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VerifiedDecision(Decision);

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Decision {
    /// The value of the offered option the human selected.
    Selected(String),
    /// The text the human typed.
    Answered(String),
    Cancelled,
}

impl VerifiedDecision {
    pub fn decision(&self) -> &Decision {
        &self.0
    }
}

/// Why a response was refused. Mapped to HTTP statuses by the route.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum RespondError {
    #[error("no pending interaction with that id")]
    NotFound,
    #[error("this approval was not addressed to the authenticated user")]
    NotOwner,
    #[error("response type {submitted:?} does not match the pending prompt type {expected:?}")]
    WrongKind { submitted: &'static str, expected: &'static str },
    #[error("the submitted choice was not offered by this prompt")]
    NotOffered,
    #[error("a text answer must not be empty")]
    EmptyText,
    #[error("the run this interaction belonged to has ended")]
    Gone,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct Key {
    execution_id: String,
    interaction_id: String,
}

struct Entry {
    owner: String,
    offer: Offer,
    prompt_type: &'static str,
    sender: oneshot::Sender<VerifiedDecision>,
}

#[derive(Default)]
pub struct InteractionRegistry {
    pending: Mutex<HashMap<Key, Entry>>,
}

#[derive(Debug, thiserror::Error)]
#[error("too many interactions are pending")]
pub struct RegistryFull;

/// What waiting produced.
#[derive(Debug, PartialEq, Eq)]
pub enum WaitOutcome {
    Decided(VerifiedDecision),
    /// Nobody answered in time. Treated by the gate exactly like a cancellation.
    TimedOut,
}

/// The suspended side of one interaction. Dropping it — timeout, client
/// disconnect, task abort — removes the pending entry, so an abandoned prompt
/// cannot be answered later.
pub struct PendingTicket {
    registry: Arc<InteractionRegistry>,
    key: Key,
    receiver: Option<oneshot::Receiver<VerifiedDecision>>,
}

impl PendingTicket {
    pub fn interaction_id(&self) -> &str {
        &self.key.interaction_id
    }

    pub async fn wait(mut self, timeout: Duration) -> WaitOutcome {
        let receiver = self.receiver.take().expect("a ticket is waited on once");
        match tokio::time::timeout(timeout, receiver).await {
            Ok(Ok(decision)) => WaitOutcome::Decided(decision),
            // Sender dropped without a decision cannot happen while the entry
            // exists; treat it, like a timeout, as no decision at all.
            Ok(Err(_)) | Err(_) => WaitOutcome::TimedOut,
        }
    }
}

impl Drop for PendingTicket {
    fn drop(&mut self) {
        self.registry.lock().remove(&self.key);
    }
}

impl InteractionRegistry {
    fn lock(&self) -> std::sync::MutexGuard<'_, HashMap<Key, Entry>> {
        self.pending.lock().unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    pub fn pending_count(&self) -> usize {
        self.lock().len()
    }

    /// Open an interaction owned by `owner` in execution `execution_id`.
    pub fn open(
        self: &Arc<Self>,
        execution_id: &str,
        owner: &TrustedCaller,
        prompt: &Prompt,
    ) -> Result<PendingTicket, RegistryFull> {
        let mut pending = self.lock();
        if pending.len() >= MAX_PENDING {
            return Err(RegistryFull);
        }
        let (sender, receiver) = oneshot::channel();
        let key = Key { execution_id: execution_id.to_string(), interaction_id: uuid::Uuid::new_v4().to_string() };
        pending.insert(
            key.clone(),
            Entry {
                owner: owner.user_id().to_string(),
                offer: prompt.offer.clone(),
                prompt_type: prompt.view.input_type,
                sender,
            },
        );
        Ok(PendingTicket { registry: Arc::clone(self), key, receiver: Some(receiver) })
    }

    // DETERMINISTIC-CONTROL: who may answer which prompt with what. See
    // docs/concepts/08-human-in-the-loop.md.
    /// Resolve a pending interaction with an authenticated human response.
    pub fn respond(
        &self,
        execution_id: &str,
        interaction_id: &str,
        responder: &TrustedCaller,
        response: &HumanResponse,
    ) -> Result<(), RespondError> {
        let key = Key { execution_id: execution_id.to_string(), interaction_id: interaction_id.to_string() };
        let mut pending = self.lock();
        let entry = pending.get(&key).ok_or(RespondError::NotFound)?;

        if entry.owner != responder.user_id() {
            return Err(RespondError::NotOwner);
        }
        if response.kind() != entry.prompt_type {
            return Err(RespondError::WrongKind { submitted: response.kind(), expected: entry.prompt_type });
        }
        let decision = match (&entry.offer, response) {
            (Offer::Choice(options), HumanResponse::Radio { selected_option }) => {
                choice_decision(options, selected_option)?
            }
            (Offer::Text, HumanResponse::Text { text }) => {
                let text = text.trim();
                if text.is_empty() {
                    return Err(RespondError::EmptyText);
                }
                if text == CANCEL_SENTINEL { Decision::Cancelled } else { Decision::Answered(text.to_string()) }
            }
            // Kinds already matched; anything else was never offered.
            _ => return Err(RespondError::NotOffered),
        };

        // Single use: the entry leaves the map before the decision is sent.
        let entry = pending.remove(&key).expect("present under the same lock");
        entry.sender.send(VerifiedDecision(decision)).map_err(|_| RespondError::Gone)
    }
}

fn choice_decision(options: &[OptionView], selected: &RadioOption) -> Result<Decision, RespondError> {
    // A cancellation is accepted only when the *whole* selection is one: a
    // sentinel value paired with a real id (or the reverse) is an attempt to
    // authorise that other field by attaching it to a sentinel.
    let cancel_id = selected.id == CANCEL_ID;
    let cancel_value = selected.value == CANCEL_SENTINEL;
    if cancel_id || cancel_value {
        return if cancel_id && cancel_value { Ok(Decision::Cancelled) } else { Err(RespondError::NotOffered) };
    }
    options
        .iter()
        .find(|option| option.id == selected.id && option.value == selected.value)
        .map(|option| Decision::Selected(option.value.clone()))
        .ok_or(RespondError::NotOffered)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn caller(user: &str) -> TrustedCaller {
        TrustedCaller::for_tests(user, Some("req"))
    }

    fn option(id: &str) -> OptionView {
        OptionView { id: id.into(), label: id.into(), value: id.into(), description: String::new() }
    }

    fn radio(id: &str, value: &str) -> HumanResponse {
        HumanResponse::Radio {
            selected_option: RadioOption {
                id: id.into(),
                label: "x".into(),
                value: value.into(),
                description: String::new(),
            },
        }
    }

    fn open_choice(registry: &Arc<InteractionRegistry>, owner: &str) -> PendingTicket {
        let prompt = Prompt::choice("Pick".into(), vec![option("low"), option("high")]);
        registry.open("exec-1", &caller(owner), &prompt).unwrap()
    }

    #[tokio::test]
    async fn the_owner_resolves_with_an_offered_choice() {
        let registry = Arc::new(InteractionRegistry::default());
        let ticket = open_choice(&registry, "alice");
        let id = ticket.interaction_id().to_string();
        registry.respond("exec-1", &id, &caller("alice"), &radio("high", "high")).unwrap();
        assert_eq!(
            ticket.wait(Duration::from_secs(1)).await,
            WaitOutcome::Decided(VerifiedDecision(Decision::Selected("high".into())))
        );
        assert_eq!(registry.pending_count(), 0);
    }

    #[tokio::test]
    async fn another_authenticated_user_cannot_answer() {
        let registry = Arc::new(InteractionRegistry::default());
        let ticket = open_choice(&registry, "alice");
        let id = ticket.interaction_id().to_string();
        assert_eq!(
            registry.respond("exec-1", &id, &caller("mallory"), &radio("high", "high")),
            Err(RespondError::NotOwner)
        );
        // Still pending: the legitimate owner can answer afterwards.
        registry.respond("exec-1", &id, &caller("alice"), &radio("low", "low")).unwrap();
        assert!(matches!(ticket.wait(Duration::from_secs(1)).await, WaitOutcome::Decided(_)));
    }

    #[tokio::test]
    async fn a_response_is_single_use() {
        let registry = Arc::new(InteractionRegistry::default());
        let ticket = open_choice(&registry, "alice");
        let id = ticket.interaction_id().to_string();
        registry.respond("exec-1", &id, &caller("alice"), &radio("high", "high")).unwrap();
        assert_eq!(
            registry.respond("exec-1", &id, &caller("alice"), &radio("high", "high")),
            Err(RespondError::NotFound)
        );
        drop(ticket);
    }

    #[test]
    fn unoffered_and_mixed_choices_are_refused() {
        let registry = Arc::new(InteractionRegistry::default());
        let ticket = open_choice(&registry, "alice");
        let id = ticket.interaction_id().to_string();
        let alice = caller("alice");
        for response in [
            radio("urgent", "urgent"),      // never offered
            radio("low", "high"),           // id from one option, value from another
            radio("cancel", "high"),        // half a cancellation
            radio("high", CANCEL_SENTINEL), // the other half
        ] {
            assert_eq!(
                registry.respond("exec-1", &id, &alice, &response),
                Err(RespondError::NotOffered),
                "{response:?}"
            );
        }
        assert_eq!(registry.pending_count(), 1, "refusals leave the prompt pending");
    }

    #[tokio::test]
    async fn a_self_consistent_cancel_is_always_accepted() {
        let registry = Arc::new(InteractionRegistry::default());
        let ticket = open_choice(&registry, "alice");
        let id = ticket.interaction_id().to_string();
        registry.respond("exec-1", &id, &caller("alice"), &radio(CANCEL_ID, CANCEL_SENTINEL)).unwrap();
        assert_eq!(
            ticket.wait(Duration::from_secs(1)).await,
            WaitOutcome::Decided(VerifiedDecision(Decision::Cancelled))
        );
    }

    #[test]
    fn the_response_kind_must_match_the_prompt() {
        let registry = Arc::new(InteractionRegistry::default());
        let ticket = open_choice(&registry, "alice");
        let id = ticket.interaction_id().to_string();
        let text = HumanResponse::Text { text: "high".into() };
        assert!(matches!(
            registry.respond("exec-1", &id, &caller("alice"), &text),
            Err(RespondError::WrongKind { .. })
        ));
        let binary = HumanResponse::BinaryChoice {
            selected_option: BinaryOption { id: "confirm".into(), label: "Confirm".into(), value: true },
        };
        assert!(matches!(
            registry.respond("exec-1", &id, &caller("alice"), &binary),
            Err(RespondError::WrongKind { .. })
        ));
    }

    #[tokio::test]
    async fn a_text_prompt_takes_text_or_the_cancel_sentinel() {
        let registry = Arc::new(InteractionRegistry::default());
        let prompt = Prompt::text("Why?".into(), "Reason".into());
        let ticket = registry.open("exec-1", &caller("alice"), &prompt).unwrap();
        let id = ticket.interaction_id().to_string();
        let alice = caller("alice");
        assert_eq!(
            registry.respond("exec-1", &id, &alice, &HumanResponse::Text { text: "  ".into() }),
            Err(RespondError::EmptyText)
        );
        registry.respond("exec-1", &id, &alice, &HumanResponse::Text { text: " Customer escalated. ".into() }).unwrap();
        assert_eq!(
            ticket.wait(Duration::from_secs(1)).await,
            WaitOutcome::Decided(VerifiedDecision(Decision::Answered("Customer escalated.".into())))
        );
    }

    #[test]
    fn a_fabricated_or_misdirected_interaction_is_not_found() {
        let registry = Arc::new(InteractionRegistry::default());
        let ticket = open_choice(&registry, "alice");
        let id = ticket.interaction_id().to_string();
        let alice = caller("alice");
        assert_eq!(
            registry.respond("exec-1", "00000000-0000-4000-8000-000000000000", &alice, &radio("high", "high")),
            Err(RespondError::NotFound)
        );
        assert_eq!(registry.respond("exec-2", &id, &alice, &radio("high", "high")), Err(RespondError::NotFound));
    }

    #[tokio::test]
    async fn an_abandoned_prompt_cannot_be_answered_later() {
        let registry = Arc::new(InteractionRegistry::default());
        let ticket = open_choice(&registry, "alice");
        let id = ticket.interaction_id().to_string();
        assert_eq!(ticket.wait(Duration::from_millis(10)).await, WaitOutcome::TimedOut);
        assert_eq!(
            registry.respond("exec-1", &id, &caller("alice"), &radio("high", "high")),
            Err(RespondError::NotFound)
        );
    }

    #[tokio::test]
    async fn concurrent_interactions_are_isolated() {
        let registry = Arc::new(InteractionRegistry::default());
        let alice =
            registry.open("exec-a", &caller("alice"), &Prompt::choice("A".into(), vec![option("low")])).unwrap();
        let bob = registry.open("exec-b", &caller("bob"), &Prompt::choice("B".into(), vec![option("high")])).unwrap();
        let (alice_id, bob_id) = (alice.interaction_id().to_string(), bob.interaction_id().to_string());
        // Each can answer only their own, with only their own options.
        assert_eq!(
            registry.respond("exec-a", &alice_id, &caller("bob"), &radio("low", "low")),
            Err(RespondError::NotOwner)
        );
        assert_eq!(
            registry.respond("exec-b", &bob_id, &caller("bob"), &radio("low", "low")),
            Err(RespondError::NotOffered)
        );
        registry.respond("exec-b", &bob_id, &caller("bob"), &radio("high", "high")).unwrap();
        registry.respond("exec-a", &alice_id, &caller("alice"), &radio("low", "low")).unwrap();
        assert_eq!(
            bob.wait(Duration::from_secs(1)).await,
            WaitOutcome::Decided(VerifiedDecision(Decision::Selected("high".into())))
        );
        assert_eq!(
            alice.wait(Duration::from_secs(1)).await,
            WaitOutcome::Decided(VerifiedDecision(Decision::Selected("low".into())))
        );
    }
}
