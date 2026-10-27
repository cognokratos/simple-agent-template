//! The execution state machine of one request.
//!
//! Explicit states rather than a handful of booleans (`blocked`, `waiting`,
//! `done`, …) that could be set in combinations nothing means. Every legal
//! move is listed in [`ExecutionState::apply`]; anything else is refused, so a
//! completed run cannot start waiting for an approval and a blocked one
//! cannot be resumed.
//!
//! ```text
//! Received ──screen──▶ ScreeningInput ──allow──▶ Running ◀──resume── AwaitingApproval
//!                          │                      │  │                    ▲
//!                          └─block──▶ Blocked     │  └───await approval───┘
//!                                                 ├──▶ Completed
//!                                                 ├──▶ Failed
//!                                                 └──▶ Cancelled (client went away)
//! ```

use std::fmt;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ExecutionState {
    Received,
    ScreeningInput,
    /// The input policy refused the request; the model was never called.
    Blocked,
    /// The agent loop is running: model turns and tool calls.
    Running,
    /// Suspended on a human prompt. The interaction id is the only handle by
    /// which the run can resume.
    AwaitingApproval {
        interaction_id: String,
    },
    Completed,
    Failed,
    Cancelled,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Transition {
    ScreenInput,
    Block,
    Allow,
    AwaitApproval { interaction_id: String },
    Resume,
    Complete,
    Fail,
    Cancel,
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("cannot {transition:?} from {from}")]
pub struct IllegalTransition {
    from: &'static str,
    transition: Transition,
}

impl ExecutionState {
    pub fn name(&self) -> &'static str {
        match self {
            Self::Received => "received",
            Self::ScreeningInput => "screening_input",
            Self::Blocked => "blocked",
            Self::Running => "running",
            Self::AwaitingApproval { .. } => "awaiting_approval",
            Self::Completed => "completed",
            Self::Failed => "failed",
            Self::Cancelled => "cancelled",
        }
    }

    pub fn is_terminal(&self) -> bool {
        matches!(self, Self::Blocked | Self::Completed | Self::Failed | Self::Cancelled)
    }

    pub fn apply(self, transition: Transition) -> Result<Self, IllegalTransition> {
        use ExecutionState as S;
        use Transition as T;
        let next = match (&self, &transition) {
            (S::Received, T::ScreenInput) => S::ScreeningInput,
            (S::ScreeningInput, T::Block) => S::Blocked,
            (S::ScreeningInput, T::Allow) => S::Running,
            (S::Running, T::AwaitApproval { interaction_id }) => {
                S::AwaitingApproval { interaction_id: interaction_id.clone() }
            }
            (S::AwaitingApproval { .. }, T::Resume) => S::Running,
            (S::Running, T::Complete) => S::Completed,
            // Any non-terminal state can fail or be cancelled.
            (state, T::Fail) if !state.is_terminal() => S::Failed,
            (state, T::Cancel) if !state.is_terminal() => S::Cancelled,
            _ => return Err(IllegalTransition { from: self.name(), transition }),
        };
        Ok(next)
    }
}

impl fmt::Display for ExecutionState {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.name())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_normal_and_approval_paths_are_legal() {
        let state = ExecutionState::Received
            .apply(Transition::ScreenInput)
            .and_then(|s| s.apply(Transition::Allow))
            .and_then(|s| s.apply(Transition::AwaitApproval { interaction_id: "i".into() }))
            .and_then(|s| s.apply(Transition::Resume))
            .and_then(|s| s.apply(Transition::Complete))
            .unwrap();
        assert_eq!(state, ExecutionState::Completed);
    }

    #[test]
    fn invalid_states_cannot_be_reached() {
        // A blocked request never reaches the model, so it cannot await approval.
        let blocked = ExecutionState::ScreeningInput.apply(Transition::Block).unwrap();
        assert!(blocked.clone().apply(Transition::AwaitApproval { interaction_id: "i".into() }).is_err());
        assert!(blocked.apply(Transition::Resume).is_err());
        // Resuming is only possible from a suspension.
        assert!(ExecutionState::Running.apply(Transition::Resume).is_err());
        // A finished run stays finished.
        assert!(ExecutionState::Completed.apply(Transition::Fail).is_err());
        assert!(ExecutionState::Cancelled.apply(Transition::Allow).is_err());
        // Input screening cannot be skipped.
        assert!(ExecutionState::Received.apply(Transition::Allow).is_err());
    }

    #[test]
    fn a_suspended_run_can_be_cancelled_by_a_disconnect() {
        let waiting = ExecutionState::AwaitingApproval { interaction_id: "i".into() };
        assert_eq!(waiting.apply(Transition::Cancel).unwrap(), ExecutionState::Cancelled);
    }
}
