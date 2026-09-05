#!/usr/bin/env python3
"""End-to-end check that a human can *initiate* a decision override.

The approval-boundary suite (`make verify-approvals`) proves the MCP enforces
every rule once a token exists. It cannot prove the interaction that produces one
behaves correctly, because it mints tokens directly. The evaluation suites cannot
either: they are all read-only, so nothing exercises the confirmation gate.

This drives the whole path with no browser: ask the agent to evaluate an ETF and
commit a decision, answer the decision prompt by choosing something *different*
from what the engine arrived at, supply the mandatory rationale, and assert the
history records a human override the model never proposed.

That last part is the point. Previously an override could only be reached if the
*model* proposed a different decision and the human ratified it; a person could
not start one. For a control over what a person will put money into, that is the
wrong direction of initiative.

The promotion target is `shortlist`, which the MCP refuses without a grounded
research note — and a model proposing `research` has no reason to have drafted one.
So the note is collected from the person, in the same flow, and this check asserts
that it is: without it a human-initiated promotion is refused for a missing argument
the *model* was supposed to supply, which means the person can only reach shortlist
where the model has already been. That is permission, not initiative, and it is the
specific failure this suite caught.

Whether the note prompt appears depends on whether the model happened to draft one,
so this module reports which path was taken rather than asserting one of them; the
rule itself is asserted deterministically, with no model, by
`check_a_human_promotion_can_be_completed_without_the_model` in
`verify_approval_tokens.py`.

This module covers the interaction half: that a decision choice was offered at
all, that every decision was on it, that choosing a different one demanded a
rationale, that the promotion was completable either way, and that every prompt
was answered. The persisted half is asserted by
`make verify-hitl-audit`, which `make verify-hitl` chains immediately afterwards.
It checks that `llm_recommendation` is not the promotion while
`human_override_decision` is present — deliberately not that `llm_recommendation`
is absent, which is a stricter proxy than the property being described and
contradicts the system prompt telling the agent to send its recommendation on
every commit.

Run with `make verify-hitl`. It needs the cluster and a model, so it is not part
of `make test`.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import urllib.request
import uuid

BASE = "http://127.0.0.1:8000"
# Reserved for this check. Not referenced by any evaluation dataset, the approval
# suite, or the demo guide.
ETF = "ESPO-XETRA"  # deterministic decision: research (score 54, capped)
# Deliberately the *more optimistic* direction: the one the model is forbidden
# from ever recommending, so reaching it proves the person started it.
OVERRIDE_TO = "shortlist"
RATIONALE = (
    "Investor-initiated override: accepting the incomplete record and the "
    "thematic concentration as a small satellite position alongside a global core."
)
#: Supplied by the person, not the model. Deliberately asserts nothing the snapshot
#: does not contain, and nothing about future return.
RESEARCH_NOTE = (
    "Investor-supplied note: shortlisted as a small satellite holding on the "
    "deterministic result and the verified metrics alone. The record is incomplete "
    "and no return is implied; no position is held."
)

ACTOR = f"hitl-check-{uuid.uuid4()}"
REQUEST_ID = str(uuid.uuid4())
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "text/event-stream",
    "Authorization": f"Bearer {os.environ['NAT_GATEWAY_API_KEY']}",
    # Normally injected by the gateway after session validation. The model can
    # never supply these; that is what makes the audit actor trustworthy.
    "x-authenticated-user-id": ACTOR,
    "x-request-id": REQUEST_ID,
}


def post(path: str, payload: dict):
    request = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(), headers=HEADERS, method="POST"
    )
    return urllib.request.urlopen(request, timeout=300)


def main() -> int:
    events: queue.Queue = queue.Queue()

    def consume() -> None:
        try:
            response = post(
                "/v1/workflow/full",
                {
                    "input_message": (
                        f"Evaluate {ETF} and then commit a review decision for it. "
                        "Call evaluate_etf first, then get_research_context, then "
                        "commit_evaluation."
                    )
                },
            )
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body and body != "[DONE]":
                    try:
                        events.put(json.loads(body))
                    except json.JSONDecodeError:
                        pass
        finally:
            events.put(None)

    threading.Thread(target=consume, daemon=True).start()

    saw_radio = False
    saw_rationale_prompt = False
    saw_note_prompt = False
    offered: list[str] = []
    answered = 0
    text_prompts: list[str] = []

    while True:
        try:
            event = events.get(timeout=240)
        except queue.Empty:
            print("FAIL: timed out waiting for the agent")
            return 1
        if event is None:
            break
        if event.get("event_type") != "interaction_required":
            continue

        prompt = event.get("prompt", {})
        if prompt.get("input_type") == "radio":
            saw_radio = True
            offered = [option["value"] for option in prompt.get("options", [])]
            option = next(
                (o for o in prompt.get("options", []) if o.get("value") == OVERRIDE_TO), None
            )
            if option is None:
                print(f"FAIL: {OVERRIDE_TO!r} was not offered; got {offered}")
                return 1
            payload = {"response": {"type": "radio", "selected_option": option}}
            print(f"  the investor chooses an override -> {OVERRIDE_TO}")
        else:
            # Two distinct text prompts, distinguished by what they ask for rather
            # than by arrival order: the rationale explains overriding the engine,
            # the note explains the candidate. They are persisted as separate
            # records, so answering one with the other's text would be wrong.
            text = str(prompt.get("text") or "")
            text_prompts.append(text)
            if "research note" in text.lower():
                saw_note_prompt = True
                payload = {"response": {"type": "text", "text": RESEARCH_NOTE}}
                print("  the investor writes the grounded research note")
            else:
                saw_rationale_prompt = True
                payload = {"response": {"type": "text", "text": RATIONALE}}
                print("  the investor supplies the mandatory rationale")
        post(event["response_url"], payload).read()
        answered += 1

    checks: list[tuple[str, bool, str]] = [
        ("a decision choice was presented", saw_radio, "the confirmation gate offered a radio prompt"),
        (
            "every decision was offered",
            {"reject", "research", "shortlist"}.issubset(set(offered)),
            f"offered={offered}",
        ),
        ("a rationale was demanded", saw_rationale_prompt, "an override triggered a text prompt"),
        (
            "the promotion was completable",
            answered in (2, 3),
            "the person supplied the grounded note themselves"
            if saw_note_prompt
            else "the model had already drafted a grounded note, so none was asked for",
        ),
        ("every interaction was answered", answered == (3 if saw_note_prompt else 2), f"answered={answered}"),
    ]
    failures = [name for name, ok, _ in checks if not ok]
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")

    if failures:
        print(f"\n{len(failures)} interaction check(s) failed")
        for text in text_prompts:
            print(f"  text prompt seen: {text[:120]!r}")
        return 1

    print(
        "\nInteraction path verified. Confirm the persisted result with:\n"
        "  make verify-hitl-audit"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
