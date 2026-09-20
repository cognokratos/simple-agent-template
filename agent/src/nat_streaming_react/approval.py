"""Human-approved state changes, as a NeMo Agent Toolkit function.

This is the agent half of an optional, opt-in capability. It is **not** enabled
in the shipped configuration: the template's sample application is read-only, and
this function only becomes reachable when it is listed in
``workflow.tool_names`` and ``HITL_APPROVAL_SECRET`` is set (see the commented
block in ``agent/config.yml``). Without both, the model has no capability to
change state and the MCP server routes no execution endpoint at all.

Shape, and why
--------------
One function per state-changing action, and each one *pauses, then applies*:

* Each action's arguments are exactly the ones that action needs, so the schema
  rejects an incomplete request before any hand-written validation runs. A
  single multi-action approval tool has to accept the union of every action's
  fields and check them by hand, and a model reliably omits whichever one the
  chosen action required.
* Authorization and effect are one step. Nothing between the human's
  confirmation and the state change depends on further model output, so a
  request can never end up approved but unapplied — and the model never gets an
  opportunity to restate what was approved.

The four checks, and who makes them
-----------------------------------
No single layer is trusted alone:

1. **Gateway** — shape, size, encoding, and the protocol-level
   confirm/cancel consistency. It cannot know which choices are legitimate for
   an arbitrary application, and deliberately does not pretend to.
2. **Interaction guard** (``interaction_guard``) — the responder owns the
   execution, and the submitted choice is one *this* prompt offered. NAT itself
   authorizes on knowledge of two UUIDs, which is not authorization.
3. **This module** — mints a token binding the action, the resource, the
   authenticated actor, the originating request, the authoritative state the
   human was shown, and the exact payload. The signing key is not available to
   the model. The payload can carry model-supplied content (a free-text
   ``note``); that content is normalized exactly once and the same value is
   shown to the human, signed, and persisted — signing something the human was
   never shown would not be a human approval of it.
4. **MCP server** — verifies the signature and every binding independently,
   re-derives the authoritative state under a row lock, re-checks the transition
   against backend policy, and applies the mutation, the nonce consumption and
   the audit insert in one transaction.

A refusal at step 4 is the control working, not a gap, and the model is told
plainly that nothing was applied.

Generalizing this
-----------------
``resource_id``, the action name, the choice vocabulary and the payload fields
are all application-owned. Adding an action means: a request model and a
registered function here, an entry in ``mutation::ACTIONS`` on the MCP side, and
whatever the mutation itself needs. Nothing in the token format or the
verification changes.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Literal

from nat.builder.builder import Builder
from nat.builder.context import Context
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function
from nat.data_models.function import FunctionBaseConfig
from nat.data_models.interactive import HumanPromptRadio
from nat.data_models.interactive import HumanPromptText
from nat.data_models.interactive import HumanResponseRadio
from nat.data_models.interactive import HumanResponseText
from nat.data_models.interactive import MultipleChoiceOption
from pydantic import BaseModel, Field

# Do not enable postponed annotations in this module. NeMo Agent Toolkit 1.8
# introspects the nested tool callable with typing.get_type_hints during startup;
# keeping the request models as concrete runtime annotations avoids losing them
# from the wrapper namespace used by FunctionInfo.from_fn.

#: The demonstration action. Must match an entry in the MCP server's
#: ``mutation::ACTIONS`` registry, which is the authority on what may be applied.
ACTION_SET_TICKET_PRIORITY = "set_ticket_priority"

TicketPriority = Literal["low", "medium", "high", "urgent"]

#: Cancellation is part of the interaction protocol, not of any application's
#: vocabulary. Shared verbatim with the gateway and the interaction guard.
CANCEL_SENTINEL = "__CANCEL__"

#: Ceiling on a minted token's lifetime. The MCP server enforces its own
#: independent ceiling, because the minter is not the trust boundary.
MAX_TOKEN_TTL_SECONDS = 1800


class SetTicketPriorityConfig(FunctionBaseConfig, name="ticket_set_priority_approval"):
    """Configuration for the approval-gated ticket priority change."""

    token_ttl_seconds: int = Field(default=600, ge=60, le=MAX_TOKEN_TTL_SECONDS)


class SetTicketPriorityRequest(BaseModel):
    """The priority change the user is being asked to authorize."""

    ticket_id: str = Field(min_length=1, description="Exact ticket identifier, e.g. TKT-1001")
    current_priority: TicketPriority = Field(
        description="The ticket's current priority, exactly as get_ticket returned it"
    )
    requested_priority: TicketPriority = Field(
        description="The priority to apply. Your recommendation only; the user chooses, "
        "and any change away from the current priority requires them to type a reason"
    )
    summary: str = Field(
        min_length=1,
        max_length=1500,
        description="Concise user-facing explanation of the proposed change",
    )
    note: str | None = Field(
        default=None,
        max_length=4000,
        description="Optional grounded note to persist with the decision, drawn from the "
        "ticket and its history",
    )


# ---------------------------------------------------------------------------
# Token minting
# ---------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def canonical_json(value: Any) -> str:
    """Serialize with sorted keys and no insignificant whitespace.

    The MCP verifier hashes the payload independently, in Rust, so both sides
    have to agree on the exact bytes. Sorted keys with compact separators is the
    one form both can produce identically. Mirrored by ``canonical_json`` in
    ``mcp-server/src/approval.rs``.
    """

    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def payload_hash(payload: Any) -> str | None:
    """Hex SHA-256 of the canonical payload, or None when there is no payload."""

    if payload is None:
        return None
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def mint_token(secret: bytes, claims: dict[str, Any]) -> str:
    """HMAC-SHA256 over the base64url payload, exactly as the verifier expects."""

    payload = canonical_json(claims).encode("utf-8")
    payload_b64 = _b64url(payload)
    signature = hmac.new(secret, payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64url(signature)}"


def approval_secret() -> bytes:
    secret_text = os.getenv("HITL_APPROVAL_SECRET", "")
    if len(secret_text) < 24:
        raise ValueError("HITL_APPROVAL_SECRET must contain at least 24 characters")
    return secret_text.encode("utf-8")


def build_claims(
    *,
    action: str,
    resource_id: str,
    actor_id: str,
    request_id: str,
    ttl_seconds: int,
    choice: str | None,
    expected_choice: str | None,
    rationale: str | None,
    payload: dict[str, Any] | None,
    now: int | None = None,
) -> dict[str, Any]:
    """Assemble the claim set. Pure, so the whole shape is testable offline."""

    if ttl_seconds < 60 or ttl_seconds > MAX_TOKEN_TTL_SECONDS:
        raise ValueError(f"token_ttl_seconds must be between 60 and {MAX_TOKEN_TTL_SECONDS}")
    issued = int(time.time()) if now is None else now
    effective_payload: dict[str, Any] = payload or {}
    return {
        "v": 1,
        "exp": issued + ttl_seconds,
        "action": action,
        "resource_id": resource_id,
        "actor_id": actor_id,
        "request_id": request_id,
        "choice": choice,
        "expected_choice": expected_choice,
        # Recorded, not trusted: the MCP re-derives it from the choice and the
        # authoritative state it reads under a lock.
        "override_requested": choice is not None and choice != expected_choice,
        "rationale": rationale,
        "payload": effective_payload,
        "payload_sha256": payload_hash(effective_payload),
        "nonce": str(uuid.uuid4()),
    }


def _identity() -> tuple[str, str]:
    """Authenticated user and request correlation, from gateway-injected headers.

    Never supplied by the model: the browser and the LLM must not be able to
    choose the identity that ends up in the append-only audit trail.
    """

    headers = Context.get().metadata.headers
    actor_id = headers.get("x-authenticated-user-id") if headers is not None else None
    request_id = headers.get("x-request-id") if headers is not None else None
    if not actor_id or not request_id:
        raise ValueError(
            "Human approval requires authenticated gateway identity and request metadata"
        )
    return actor_id, request_id


# ---------------------------------------------------------------------------
# Applying the approved change
# ---------------------------------------------------------------------------


def execute_url() -> str | None:
    """Internal MCP endpoint that applies an approved mutation.

    Derived from the MCP URL the agent already talks to, so there is nothing
    extra to configure. The signed token goes straight here: nothing
    model-visible ever carries an approval reference.
    """

    mcp_url = os.getenv("TICKETS_MCP_URL", "")
    if not mcp_url:
        return None
    return mcp_url.rstrip("/").removesuffix("/mcp") + "/approvals/execute"


def _execute_blocking(token: str, request_id: str) -> dict[str, Any]:
    url = execute_url()
    api_key = os.getenv("MCP_API_KEY", "")
    if not url or not api_key:
        raise ValueError("MCP execution endpoint is not configured")

    payload = json.dumps({"approval_token": token, "request_id": request_id}).encode("utf-8")
    http_request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"content-type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(http_request, timeout=30) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:400]
        raise ValueError(f"The approved change was rejected: {detail}") from error
    except (urllib.error.URLError, ValueError, TimeoutError) as error:
        raise ValueError(f"The approved change could not be applied: {error}") from error
    return body if isinstance(body, dict) else {}


async def _execute(token: str, request_id: str) -> dict[str, Any]:
    """Apply the approved mutation without blocking the event loop.

    ``urlopen`` is a synchronous socket read, and NAT serves every request on one
    asyncio loop. Calling it inline stalls the whole process for the duration:
    other users' token streams stop mid-answer, and guardrail evaluation and the
    telemetry pipeline stop with them. Usually that is milliseconds, but the
    timeout is 30 seconds and the MCP mutation path takes a row lock, so two
    approvals racing on one resource is exactly the case that makes it visible.
    """

    return await asyncio.to_thread(_execute_blocking, token, request_id)


def approval_result(
    *, resource_id: str, action: str, request_id: str, committed: bool, result: Any
) -> str:
    """What the model is told after an approved mutation is attempted.

    ``next_step`` is conditional, because an approval is not an outcome. The MCP
    re-checks policy *after* the human approves, so a legitimately approved
    change can still be refused. Reporting success either way is how an agent
    ends up telling a user that a refused change was applied.
    """

    return json.dumps(
        {
            "approved": True,
            "resource_id": resource_id,
            "action": action,
            "request_id": request_id,
            "committed": committed,
            "result": result,
            "next_step": (
                "The change is already applied. Report the outcome."
                if committed
                else "The change was REFUSED and NOTHING was applied. The record is "
                "unchanged. Report the refusal and the reason given in result; never "
                "say or imply that any change occurred."
            ),
        },
        separators=(",", ":"),
    )


def cancelled(resource_id: str, action: str, message: str) -> str:
    return json.dumps(
        {"approved": False, "resource_id": resource_id, "action": action, "message": message},
        separators=(",", ":"),
    )


# ---------------------------------------------------------------------------
# Prompting
# ---------------------------------------------------------------------------


def priority_options(current_priority: str, allowed: tuple[str, ...]) -> list[MultipleChoiceOption]:
    """Options offered for a priority change, plus cancel.

    Every allowed priority is offered, including the current one, so the person
    can decline the change without abandoning the conversation. The label makes
    clear which is a change and which is not; the description states the
    consequence rather than restating the name.
    """

    options = [
        MultipleChoiceOption(
            id=priority,
            value=priority,
            label=(f"Keep — {priority}" if priority == current_priority else f"Change to — {priority}"),
            description=(
                "No change is applied."
                if priority == current_priority
                else "Requires a reason, recorded against your identity."
            ),
        )
        for priority in allowed
    ]
    options.append(
        MultipleChoiceOption(
            id="cancel",
            value=CANCEL_SENTINEL,
            label="Cancel",
            description="Change nothing.",
        )
    )
    return options


def needs_rationale(choice: str, current_priority: str) -> bool:
    """Whether the human must state a reason.

    Pure, so the rule can be asserted without a model, an interaction manager or
    a running cluster. The MCP re-derives the same condition and refuses without
    a reason, so this prompt collects it rather than letting the change be
    refused afterwards for something the person was never asked for.
    """

    return choice != current_priority


async def _ask_choice(
    prompt_text: str, current_priority: str, allowed: tuple[str, ...]
) -> str | None:
    """Let the user pick a priority. Returns None on cancellation."""

    response = await Context.get().user_interaction_manager.prompt_user_input(
        HumanPromptRadio(text=prompt_text, options=priority_options(current_priority, allowed))
    )
    if not isinstance(response.content, HumanResponseRadio):
        raise ValueError("Expected a priority choice")
    chosen = (response.content.selected_option.value or "").strip()
    if chosen == CANCEL_SENTINEL:
        return None
    if chosen not in allowed:
        # The interaction guard already rejects an unoffered choice before the
        # workflow resumes; this is the second, local check.
        raise ValueError(f"Unknown priority choice: {chosen!r}")
    return chosen


async def _ask_rationale(
    choice: str, current_priority: str, context: list[str], note: str
) -> str | None:
    """Collect the mandatory reason for a change. Returns None on cancellation."""

    lines = [
        *context,
        f"The ticket is currently '{current_priority}' priority. You are changing it to "
        f"'{choice}'. This is recorded in the append-only audit trail against your "
        "identity and requires a reason.",
        *([_note_disclosure(note)] if note else []),
    ]
    response = await Context.get().user_interaction_manager.prompt_user_input(
        HumanPromptText(
            text="\n".join(lines),
            required=True,
            placeholder="Why should this ticket's priority change?",
        )
    )
    if not isinstance(response.content, HumanResponseText):
        raise ValueError("Expected a text response for the reason")
    rationale = (response.content.text or "").strip()
    if rationale == CANCEL_SENTINEL:
        return None
    if not rationale:
        raise ValueError("Changing the priority requires a non-empty reason")
    return rationale


def model_supplied_note(request: SetTicketPriorityRequest) -> str:
    """The note as it will be shown, signed and persisted — computed once.

    Every consumer (both prompts and ``build_claims``) is handed this exact
    string, never a fresh read of ``request.note``. Signing content the human
    was never shown is how "approved" stops meaning anything; the fix is
    structural, not a check to remember to run: there is only one place this
    value is derived; a human is the only source of anything before this
    exists, and nothing here re-derives it once they've reviewed it.
    """

    return (request.note or "").strip()


def _note_disclosure(note: str) -> str:
    """One line making the note's provenance and fate unmistakable."""

    return (
        f"Model-supplied note (not verified by a human; will be signed and "
        f"recorded verbatim if you approve): {note}"
    )


def prompt_text(request: SetTicketPriorityRequest, note: str) -> str:
    lines = [
        f"Ticket {request.ticket_id} is currently '{request.current_priority}' priority.",
        f"The assistant proposes '{request.requested_priority}'.",
        f"Summary: {request.summary}",
        *([_note_disclosure(note)] if note else []),
        "Choose the priority to record. Any change requires a reason.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@register_function(config_type=SetTicketPriorityConfig)
async def ticket_set_priority_approval(config: SetTicketPriorityConfig, builder: Builder):
    """Approval-gated ticket priority change."""

    allowed: tuple[str, ...] = ("low", "medium", "high", "urgent")

    async def _response_fn(request: SetTicketPriorityRequest) -> str:
        actor_id, request_id = _identity()
        resource_id = request.ticket_id.strip()
        # Computed once, from the immutable request, before any prompt is
        # shown. Every prompt the human sees, and the claims that get signed
        # and persisted, all read this same value — never request.note again.
        note = model_supplied_note(request)

        choice = await _ask_choice(prompt_text(request, note), request.current_priority, allowed)
        if choice is None:
            return cancelled(resource_id, ACTION_SET_TICKET_PRIORITY, "The user cancelled.")

        rationale = None
        if needs_rationale(choice, request.current_priority):
            rationale = await _ask_rationale(
                choice,
                request.current_priority,
                [f"Ticket {resource_id}: {request.summary}"],
                note,
            )
            if rationale is None:
                return cancelled(resource_id, ACTION_SET_TICKET_PRIORITY, "The user cancelled.")
        elif choice == request.current_priority:
            # Keeping the current priority is a decision, not a mutation.
            # Nothing is minted, so nothing can be applied.
            return cancelled(
                resource_id,
                ACTION_SET_TICKET_PRIORITY,
                f"The user kept the current priority '{choice}'. Nothing was changed.",
            )

        claims = build_claims(
            action=ACTION_SET_TICKET_PRIORITY,
            resource_id=resource_id,
            actor_id=actor_id,
            request_id=request_id,
            ttl_seconds=config.token_ttl_seconds,
            choice=choice,
            expected_choice=request.current_priority,
            rationale=rationale,
            payload={"note": note} if note else {},
        )
        outcome = await _execute(mint_token(approval_secret(), claims), request_id)
        return approval_result(
            resource_id=resource_id,
            action=ACTION_SET_TICKET_PRIORITY,
            request_id=request_id,
            committed=bool(outcome.get("ok")),
            result=outcome.get("result"),
        )

    yield FunctionInfo.create(
        single_fn=_response_fn,
        description=(
            "Change a ticket's priority. Requires explicit human approval: the user is "
            "shown the current priority and chooses what to record, and any change "
            "requires them to type a reason. Call it with the ticket's exact current "
            "priority from get_ticket. You cannot approve on the user's behalf, and you "
            "must never claim a change was applied unless the result says committed."
        ),
    )
