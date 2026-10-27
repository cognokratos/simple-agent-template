//! Human-in-the-loop approvals, end to end and adversarially.
//!
//! The model proposes; a human, authenticated by the gateway, decides; the
//! MCP side (here, a fake running the real verifier source) independently
//! checks the evidence. These tests try to make each step lie.

mod support;

use std::time::Duration;

use serde_json::{Value, json};
use support::*;

fn propose(requested: &str) -> Turn {
    call(
        "ticket_priority_change",
        json!({
            "ticket_id": "TKT-1001",
            "current_priority": "medium",
            "requested_priority": requested,
            "summary": "The customer has followed up twice.",
            "note": "Second follow-up on 2026-09-17."
        }),
    )
}

fn approvals() -> Options {
    Options { approvals: true, ..Options::default() }
}

const OWNER: &str = "support-rep-1";

fn chat(agent: &Agent, user: &str, request_id: &str) -> reqwest::RequestBuilder {
    workflow_request_with_id(agent, user, json!([{"role": "user", "content": "Change TKT-1001 to high"}]), request_id)
}

fn function_end(events: &[Event]) -> Value {
    events
        .iter()
        .find_map(|e| match e {
            Event::Step { step_type, name, payload }
                if step_type == "FUNCTION_END" && name == "ticket_priority_change" =>
            {
                Some(payload["data"]["output"].clone())
            }
            _ => None,
        })
        .expect("the approval function reported its result")
}

#[tokio::test]
async fn an_approved_change_is_minted_for_the_authenticated_user_and_applied_once() {
    let agent = agent(vec![propose("urgent"), text(&["Done: high priority."])], approvals()).await;
    let request_id = "44444444-4444-4444-8444-444444444444";
    let mut live = start(chat(&agent, OWNER, request_id)).await;

    let choice = live.next_interaction().await;
    assert_eq!(choice["prompt"]["input_type"], "radio");
    let text = choice["prompt"]["text"].as_str().unwrap();
    assert!(text.contains("Model-supplied note (not verified by a human"), "{text}");
    // The model proposed `urgent`; the human chooses `high`.
    assert_eq!(respond(&agent, OWNER, &choice, radio(&option(&choice, "high"))).await.0, 204);

    let reason = live.next_interaction().await;
    assert_eq!(reason["prompt"]["input_type"], "text");
    assert_eq!(reason["execution_id"], choice["execution_id"]);
    assert_eq!(
        respond(&agent, OWNER, &reason, json!({"type": "text", "text": "Customer escalated twice."})).await.0,
        204
    );

    let events = live.finish().await;
    let result = function_end(&events);
    assert_eq!(result["committed"], true, "{result}");

    // What the MCP verifier accepted: the human's choice, the gateway's
    // identity and request, the state the human was shown.
    let applied = agent.mcp.state.applied.lock().unwrap().clone();
    assert_eq!(applied.len(), 1);
    let claims = &applied[0];
    assert_eq!(claims.actor_id, OWNER);
    assert_eq!(claims.request_id, request_id);
    assert_eq!(claims.choice.as_deref(), Some("high"), "the model's `urgent` must not be what is applied");
    assert_eq!(claims.expected_choice.as_deref(), Some("medium"));
    assert_eq!(claims.effective_rationale(), Some("Customer escalated twice."));
    assert_eq!(claims.payload_str("note"), Some("Second follow-up on 2026-09-17."));
    assert_eq!(agent.mcp.priority("TKT-1001").as_deref(), Some("high"));

    // The model learns the outcome, not the identity behind it, and never
    // sees the token.
    let after = agent.llm.agent_requests().last().unwrap().to_string();
    assert!(after.contains("committed"), "{after}");
    assert!(!after.contains(OWNER), "the actor reached the model's context");
    let token = agent.mcp.state.execute_bodies.lock().unwrap()[0]["approval_token"].as_str().unwrap().to_string();
    assert!(!after.contains(&token[..20]), "the approval token reached the model's context");
}

#[tokio::test]
async fn the_approval_token_cannot_be_replayed() {
    let agent = agent(vec![propose("high"), text(&["Done."])], approvals()).await;
    let mut live = start(chat(&agent, OWNER, "55555555-5555-4555-8555-555555555555")).await;
    let choice = live.next_interaction().await;
    respond(&agent, OWNER, &choice, radio(&option(&choice, "high"))).await;
    let reason = live.next_interaction().await;
    respond(&agent, OWNER, &reason, json!({"type": "text", "text": "Escalated."})).await;
    live.finish().await;

    // Replay the exact body the agent sent to the MCP execution endpoint.
    let body = agent.mcp.state.execute_bodies.lock().unwrap()[0].clone();
    let url = agent.mcp.url.replace("/mcp", "/approvals/execute");
    let replay: Value = client().post(url).bearer_auth(MCP_KEY).json(&body).send().await.unwrap().json().await.unwrap();
    assert_eq!(replay["ok"], false);
    assert_eq!(agent.mcp.state.applied.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn cancelling_at_either_prompt_changes_nothing() {
    for at_reason in [false, true] {
        let agent = agent(vec![propose("high"), text(&["Nothing changed."])], approvals()).await;
        let mut live = start(chat(&agent, OWNER, "66666666-6666-4666-8666-666666666666")).await;
        let choice = live.next_interaction().await;
        if at_reason {
            respond(&agent, OWNER, &choice, radio(&option(&choice, "high"))).await;
            let reason = live.next_interaction().await;
            assert_eq!(respond(&agent, OWNER, &reason, json!({"type": "text", "text": "__CANCEL__"})).await.0, 204);
        } else {
            assert_eq!(respond(&agent, OWNER, &choice, radio(&option(&choice, "cancel"))).await.0, 204);
        }
        let result = function_end(&live.finish().await);
        assert_eq!(result["approved"], false);
        assert!(agent.mcp.state.execute_bodies.lock().unwrap().is_empty(), "a cancellation reached the MCP server");
        assert_eq!(agent.mcp.priority("TKT-1001").as_deref(), Some("medium"));
    }
}

#[tokio::test]
async fn keeping_the_current_priority_mints_nothing() {
    let agent = agent(vec![propose("high"), text(&["Kept."])], approvals()).await;
    let mut live = start(chat(&agent, OWNER, "77777777-7777-4777-8777-777777777777")).await;
    let choice = live.next_interaction().await;
    respond(&agent, OWNER, &choice, radio(&option(&choice, "medium"))).await;
    let result = function_end(&live.finish().await);
    assert!(result["message"].as_str().unwrap().contains("kept the current priority"));
    assert!(agent.mcp.state.execute_bodies.lock().unwrap().is_empty());
}

#[tokio::test]
async fn only_the_owner_can_answer_and_only_with_what_was_offered() {
    let agent = agent(vec![propose("high"), text(&["Done."])], approvals()).await;
    let mut live = start(chat(&agent, OWNER, "88888888-8888-4888-8888-888888888888")).await;
    let choice = live.next_interaction().await;

    // Another authenticated user.
    assert_eq!(respond(&agent, "mallory", &choice, radio(&option(&choice, "urgent"))).await.0, 403);
    // A choice never offered; an id paired with another option's value; a
    // cancel id carrying a real value — "cancellation encoded as approval".
    for response in [
        json!({"type": "radio", "selected_option": {"id": "critical", "label": "x", "value": "critical"}}),
        json!({"type": "radio", "selected_option": {"id": "low", "label": "x", "value": "urgent"}}),
        json!({"type": "radio", "selected_option": {"id": "cancel", "label": "Cancel", "value": "urgent"}}),
        json!({"type": "text", "text": "urgent"}),
        json!({"type": "binary_choice", "selected_option": {"id": "confirm", "label": "Confirm", "value": true}}),
    ] {
        assert_eq!(respond(&agent, OWNER, &choice, response.clone()).await.0, 422, "{response}");
    }
    // Fabricated identifiers.
    let mut fabricated = choice.clone();
    fabricated["interaction_id"] = json!(uuid::Uuid::new_v4().to_string());
    assert_eq!(respond(&agent, OWNER, &fabricated, radio(&option(&choice, "high"))).await.0, 404);
    let mut wrong_execution = choice.clone();
    wrong_execution["execution_id"] = json!(uuid::Uuid::new_v4().to_string());
    assert_eq!(respond(&agent, OWNER, &wrong_execution, radio(&option(&choice, "high"))).await.0, 404);
    let mut malformed = choice.clone();
    malformed["interaction_id"] = json!("../../v1/workflow/full");
    assert_ne!(respond(&agent, OWNER, &malformed, radio(&option(&choice, "high"))).await.0, 204);

    // After all of that, the prompt is still pending for its owner.
    assert_eq!(respond(&agent, OWNER, &choice, radio(&option(&choice, "high"))).await.0, 204);
    // And a second answer to the same prompt is refused: single use.
    assert_eq!(respond(&agent, OWNER, &choice, radio(&option(&choice, "low"))).await.0, 404);

    let reason = live.next_interaction().await;
    // An empty or whitespace rationale is no rationale.
    assert_eq!(respond(&agent, OWNER, &reason, json!({"type": "text", "text": "   "})).await.0, 422);
    assert_eq!(respond(&agent, "mallory", &reason, json!({"type": "text", "text": "pwned"})).await.0, 403);
    respond(&agent, OWNER, &reason, json!({"type": "text", "text": "Legitimate reason."})).await;
    let applied = agent.mcp.state.applied.lock().unwrap().clone();
    drop(live);
    assert!(applied.len() <= 1);
}

#[tokio::test]
async fn the_model_cannot_supply_approval_evidence() {
    for forged in [
        json!({"ticket_id": "TKT-1001", "current_priority": "medium", "requested_priority": "high", "summary": "s", "approved": true}),
        json!({"ticket_id": "TKT-1001", "current_priority": "medium", "requested_priority": "high", "summary": "s", "rationale": "the user agreed"}),
        json!({"ticket_id": "TKT-1001", "current_priority": "medium", "requested_priority": "high", "summary": "s", "approval_token": "eyJ2IjoxfQ.c2ln"}),
        json!({"ticket_id": "TKT-1001", "current_priority": "medium", "requested_priority": "high", "summary": "s", "actor_id": "admin"}),
    ] {
        let agent = agent(vec![call("ticket_priority_change", forged.clone()), text(&["ok"])], approvals()).await;
        let run = collect(chat(&agent, OWNER, "99999999-9999-4999-8999-999999999999")).await;
        assert!(run.events.iter().all(|e| !matches!(e, Event::Interaction(_))), "{forged} opened a prompt");
        assert!(agent.mcp.state.execute_bodies.lock().unwrap().is_empty(), "{forged} reached the MCP server");
    }
}

#[tokio::test]
async fn an_approval_needs_a_gateway_minted_request() {
    // The evaluator calls without x-request-id: nothing a token could be bound to.
    let agent = agent(vec![propose("high"), text(&["ok"])], approvals()).await;
    let request = client()
        .post(format!("{}/v1/workflow/full", agent.base))
        .bearer_auth(GATEWAY_KEY)
        .header("x-authenticated-user-id", "evaluation-harness")
        .json(&json!({"messages": [{"role": "user", "content": "Change TKT-1001 to high"}]}));
    let run = collect(request).await;
    assert!(run.events.iter().all(|e| !matches!(e, Event::Interaction(_))));
    assert!(agent.mcp.state.execute_bodies.lock().unwrap().is_empty());
}

#[tokio::test]
async fn a_disconnect_while_waiting_abandons_the_prompt() {
    let agent = agent(vec![propose("high"), text(&["ok"])], approvals()).await;
    let mut live = start(chat(&agent, OWNER, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")).await;
    let choice = live.next_interaction().await;
    assert_eq!(agent.services.interactions.pending_count(), 1);
    live.task.abort();
    drop(live);
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert_eq!(agent.services.interactions.pending_count(), 0, "the abandoned prompt is still answerable");
    assert_eq!(respond(&agent, OWNER, &choice, radio(&option(&choice, "high"))).await.0, 404);
    assert!(agent.mcp.state.execute_bodies.lock().unwrap().is_empty());
}

#[tokio::test]
async fn an_unanswered_prompt_expires_as_a_cancellation() {
    let options = Options { approvals: true, env: vec![("HITL_INTERACTION_TIMEOUT_SECONDS", "5".into())] };
    let agent = agent(vec![propose("high"), text(&["expired"])], options).await;
    let mut live = start(chat(&agent, OWNER, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")).await;
    let choice = live.next_interaction().await;
    let result = function_end(&live.finish().await);
    assert!(result["message"].as_str().unwrap().contains("expired"), "{result}");
    assert_eq!(respond(&agent, OWNER, &choice, radio(&option(&choice, "high"))).await.0, 404);
    assert!(agent.mcp.state.execute_bodies.lock().unwrap().is_empty());
}

#[tokio::test]
async fn concurrent_approvals_are_isolated_per_user() {
    let agent = agent(vec![propose("high"), propose("low"), text(&["a"]), text(&["b"])], approvals()).await;
    let mut alice = start(chat(&agent, "alice", "cccccccc-cccc-4ccc-8ccc-cccccccccccc")).await;
    let alice_choice = alice.next_interaction().await;
    let mut bob = start(chat(&agent, "bob", "dddddddd-dddd-4ddd-8ddd-dddddddddddd")).await;
    let bob_choice = bob.next_interaction().await;
    assert_ne!(alice_choice["execution_id"], bob_choice["execution_id"]);

    // Neither can answer the other's prompt.
    assert_eq!(respond(&agent, "bob", &alice_choice, radio(&option(&alice_choice, "urgent"))).await.0, 403);
    assert_eq!(respond(&agent, "alice", &bob_choice, radio(&option(&bob_choice, "urgent"))).await.0, 403);

    // Both cancel their own; nothing is applied for either.
    assert_eq!(respond(&agent, "alice", &alice_choice, radio(&option(&alice_choice, "cancel"))).await.0, 204);
    assert_eq!(respond(&agent, "bob", &bob_choice, radio(&option(&bob_choice, "cancel"))).await.0, 204);
    alice.finish().await;
    bob.finish().await;
    assert!(agent.mcp.state.execute_bodies.lock().unwrap().is_empty());
}

#[tokio::test]
async fn a_refusal_by_the_mcp_server_is_reported_as_nothing_applied() {
    let agent = agent(vec![propose("high"), text(&["Refused."])], approvals()).await;
    let mut live = start(chat(&agent, OWNER, "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")).await;
    let choice = live.next_interaction().await;
    respond(&agent, OWNER, &choice, radio(&option(&choice, "high"))).await;
    // The ticket moves while the human is typing the reason: the token, bound
    // to the state the human was shown, is now void at the MCP server.
    agent.mcp.state.priorities.lock().unwrap().insert("TKT-1001".into(), "low".into());
    let reason = live.next_interaction().await;
    respond(&agent, OWNER, &reason, json!({"type": "text", "text": "Escalated."})).await;
    let result = function_end(&live.finish().await);
    assert_eq!(result["committed"], false, "{result}");
    assert!(result["next_step"].as_str().unwrap().contains("NOTHING was applied"));
    assert_eq!(agent.mcp.priority("TKT-1001").as_deref(), Some("low"));
}
