"""Human-in-the-loop ETF research mutations as NeMo Agent Toolkit functions.

There is one function per state-changing action. Each pauses the workflow with the
native interaction manager, and on confirmation mints a short-lived HMAC approval
token and applies the change through the Rust MCP server, which verifies the token
independently before touching any state.

Two properties come from this shape:

* Each action's arguments are exactly the ones that action needs, so the schema
  rejects an incomplete request before any hand-written validation runs. A single
  multi-action approval tool had to accept a union of nine fields and check them by
  hand, and the model reliably omitted whichever one the chosen action required.
* Authorization and effect are one step. Nothing between the human's confirmation
  and the state change depends on further model output, so a candidate can never
  end up approved but unchanged.

The MCP mutation tools stay registered and approval-gated, but are not exposed to
the model: it has no capability to change state except through these functions.

None of these actions buys, sells or holds anything. This system ends at decision
support.
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
from typing import Literal

from nat.builder.builder import Builder
from nat.builder.context import Context
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function
from nat.data_models.function import FunctionBaseConfig
from nat.data_models.interactive import BinaryHumanPromptOption
from nat.data_models.interactive import HumanPromptBinary
from nat.data_models.interactive import HumanPromptRadio
from nat.data_models.interactive import HumanPromptText
from nat.data_models.interactive import HumanResponseBinary
from nat.data_models.interactive import HumanResponseRadio
from nat.data_models.interactive import HumanResponseText
from nat.data_models.interactive import MultipleChoiceOption
from pydantic import BaseModel, Field

# Do not enable postponed annotations in this module. NeMo Agent Toolkit 1.8
# introspects the nested tool callable with typing.get_type_hints during startup;
# keeping the request models as concrete runtime annotations avoids losing them
# from the wrapper namespace used by FunctionInfo.from_fn.
Decision = Literal["reject", "research", "shortlist"]

# reject < research < shortlist measures increasing investment attractiveness, so
# the direction a model may move is *down*. It may counsel caution; it may never
# promote a candidate on its own.
#
# It also never sets the default. The decision offered to the human is always the
# deterministic one: an earlier revision presented `llm_recommendation or
# rules_decision`, which meant that with the engine at shortlist and the model at
# research, a person choosing the engine's own answer was recorded as overriding
# the system. A model that cannot promote must not be able to demote either.
_RANKS = {"reject": 0, "research": 1, "shortlist": 2}


class CommitEvaluationConfig(FunctionBaseConfig, name="etf_commit_evaluation"):
    """Configuration for the approval-gated initial review decision."""

    token_ttl_seconds: int = Field(default=600, ge=60, le=1800)


class ShortlistEtfConfig(FunctionBaseConfig, name="etf_shortlist_etf"):
    """Configuration for the approval-gated shortlist action."""

    token_ttl_seconds: int = Field(default=600, ge=60, le=1800)


class AssignEtfConfig(FunctionBaseConfig, name="etf_assign_etf"):
    """Configuration for the approval-gated research-owner assignment."""

    token_ttl_seconds: int = Field(default=600, ge=60, le=1800)


class CommitEvaluationRequest(BaseModel):
    """Review decision the user is being asked to authorize."""

    etf_id: str = Field(min_length=1, description="Exact canonical etf_id, e.g. VWCE-XETRA")
    rules_decision: Decision = Field(
        description="Deterministic decision returned by evaluate_etf"
    )
    requested_decision: Decision = Field(
        description="Final decision to apply. Equal to rules_decision, or the user's explicit "
        "override of it. Anything other than rules_decision is a human override and the user "
        "will be required to type a rationale"
    )
    llm_recommendation: Decision | None = Field(
        default=None,
        description="Your recommendation, recorded as advisory context only. May equal "
        "rules_decision or be more conservative (reject < research < shortlist); it may never be "
        "more optimistic. It does not change the default decision either way",
    )
    research_note: str | None = Field(
        default=None,
        description="Grounded research note to persist. REQUIRED when requested_decision is "
        "shortlist. Draft it from get_research_context",
    )
    summary: str = Field(
        min_length=1,
        max_length=1500,
        description="Concise user-facing explanation of the proposed decision",
    )


class ShortlistEtfRequest(BaseModel):
    """Shortlist action the user is being asked to authorize."""

    etf_id: str = Field(min_length=1, description="Exact canonical etf_id")
    rules_decision: Decision = Field(
        description="Deterministic decision returned by evaluate_etf"
    )
    research_note: str = Field(
        min_length=1,
        description="Grounded research note to persist, drafted from get_research_context",
    )
    summary: str = Field(
        min_length=1,
        max_length=1500,
        description="Concise user-facing explanation of why this is an investment candidate",
    )


class AssignEtfRequest(BaseModel):
    """Research-owner assignment the user is being asked to authorize."""

    etf_id: str = Field(min_length=1, description="Exact canonical etf_id")
    assignee: str = Field(
        min_length=1,
        description="Exact person or queue that will own the next research decision",
    )
    summary: str = Field(
        min_length=1,
        max_length=1500,
        description="Concise user-facing explanation of the assignment",
    )


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _research_note_hash(text: str | None) -> str | None:
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _mint_token(secret: bytes, claims: dict[str, object]) -> str:
    payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = _b64url(payload)
    signature = hmac.new(secret, payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64url(signature)}"


def _secret() -> bytes:
    secret_text = os.getenv("HITL_APPROVAL_SECRET", "")
    if len(secret_text) < 24:
        raise ValueError("HITL_APPROVAL_SECRET must contain at least 24 characters")
    return secret_text.encode("utf-8")


def _identity() -> tuple[str, str]:
    """Authenticated user and request correlation, from gateway-injected headers.

    Never supplied by the model: the browser and the LLM must not be able to choose
    the identity that ends up in the append-only history.
    """
    headers = Context.get().metadata.headers
    actor_id = headers.get("x-authenticated-user-id") if headers is not None else None
    request_id = headers.get("x-request-id") if headers is not None else None
    if not actor_id or not request_id:
        raise ValueError(
            "Human approval requires authenticated gateway identity and request metadata"
        )
    return actor_id, request_id


def _execute_url() -> str | None:
    """Internal MCP endpoint that applies an approved mutation.

    The signed token goes straight here: nothing model-visible ever carries an
    approval reference, so there is no need to shorten or indirect it.

    Derived from the MCP URL the agent already talks to, so there is nothing extra
    to configure.
    """
    mcp_url = os.getenv("ETF_MCP_URL", "")
    if not mcp_url:
        return None
    return mcp_url.rstrip("/").removesuffix("/mcp") + "/approvals/execute"


def _execute_blocking(token: str, request_id: str) -> dict:
    """Apply the mutation the token authorizes, via the MCP.

    Synchronous on purpose, and never called from the event loop directly — see
    :func:`_execute`.
    """
    url = _execute_url()
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


async def _execute(token: str, request_id: str) -> dict:
    """Apply the approved mutation without blocking the event loop.

    ``urlopen`` is a synchronous socket read, and NAT serves every request on one
    asyncio loop. Calling it inline stalls the whole process for the duration:
    other users' token streams stop mid-answer, guardrail evaluation and the
    telemetry pipeline stop with them. Usually that is milliseconds, but the
    timeout is 30 seconds and the MCP mutation path takes a row lock, so two
    approvals racing on one ETF are exactly the case that makes it visible.
    """
    return await asyncio.to_thread(_execute_blocking, token, request_id)


def _approval_result(
    *, etf_id: str, action: str, request_id: str, committed: bool, result: object
) -> str:
    """What the model is told after an approved mutation is attempted.

    `next_step` is conditional, because an approval is not an outcome. The MCP
    re-checks the hard constraints *after* the human approves, so a legitimately
    approved change can still be refused — a non-UCITS shortlist, for instance.
    This used to say the change was applied either way, and the agent duly told a
    user that a refused shortlist had been "applied" while also reporting the
    refusal in the same sentence. Telling someone an investment decision was
    recorded when it was not is the wrong direction to fail in.
    """
    return json.dumps(
        {
            "approved": True,
            "etf_id": etf_id,
            "action": action,
            "request_id": request_id,
            "committed": committed,
            "result": result,
            "next_step": (
                "The change is already applied. Report the outcome. It is a research "
                "decision only: nothing was bought, sold or held."
                if committed
                else "The change was REFUSED and NOTHING was applied. The candidate is "
                "unchanged. Report the refusal and the reason given in result; never "
                "say or imply that any change occurred."
            ),
        },
        separators=(",", ":"),
    )


def _cancelled(etf_id: str, action: str, message: str) -> str:
    return json.dumps(
        {"approved": False, "etf_id": etf_id, "action": action, "message": message},
        separators=(",", ":"),
    )


def _option_description(
    decision: str, *, rules_decision: str, note_available: bool
) -> str:
    if decision == rules_decision:
        return "The deterministic engine's decision. Applied with no override."
    if decision == "shortlist" and not note_available:
        # The MCP refuses a shortlist with no grounded research note. It is
        # collected here rather than being reported as a refusal afterwards: a
        # promotion the person cannot complete unless the model happened to
        # pre-draft a note is not initiative, it is permission.
        return (
            "Requires a rationale, and a grounded research note — no note has been "
            "drafted yet, so you will be asked for one."
        )
    return "Requires a rationale, recorded against your identity."


async def _ask_rationale(
    decision: str, rules_decision: str, context: list[str] | None = None
) -> tuple[bool, str | None]:
    """Collect the mandatory rationale for an override. (approved, rationale).

    `context` carries the same lines a confirmation prompt would show. Without it
    this prompt asks someone to justify an investment decision while telling them
    only its name — which is exactly what the shortlist-promotion path used to do,
    having assembled the context and then dropped it on the floor.
    """
    lines = [
        *(context or []),
        f"The deterministic engine decided '{rules_decision}'. You are overriding it to "
        f"'{decision}'. This is recorded in the append-only history against your identity "
        "and requires a rationale.",
        "This records a research decision only. Nothing is bought, sold or held.",
    ]
    response = await Context.get().user_interaction_manager.prompt_user_input(
        HumanPromptText(
            text="\n".join(lines),
            required=True,
            placeholder="Explain why the deterministic decision should be overridden",
        )
    )
    if not isinstance(response.content, HumanResponseText):
        raise ValueError("Expected a text response for the override rationale")
    rationale = (response.content.text or "").strip()
    if rationale == "__CANCEL__":
        return False, None
    if not rationale:
        raise ValueError("A human override requires a non-empty rationale")
    return True, rationale


async def _ask_research_note(etf_id: str) -> tuple[bool, str | None]:
    """Collect the grounded research note a shortlist requires. (approved, note).

    Only reached when the human chooses `shortlist` and the model drafted no note —
    which is the ordinary case when the engine said `research`, because a model
    proposing `research` has no reason to draft one.

    Without this prompt the human's own promotion is refused by the MCP for a
    missing argument the *model* was supposed to supply, so the person can only
    reach shortlist when the model has already been there. That makes a control
    the project describes as human-initiated depend on model behaviour. The note is
    collected from the person who is actually asserting the candidate.
    """
    response = await Context.get().user_interaction_manager.prompt_user_input(
        HumanPromptText(
            text=(
                f"Shortlisting {etf_id} records it as an investment candidate, which "
                "requires a grounded research note. It is persisted verbatim and is "
                "visible to anyone reading this candidate's history.\n"
                "Base it on the verified metrics and the deterministic result. Nothing "
                "is bought, sold or held."
            ),
            required=True,
            placeholder="Why is this fund an investment candidate?",
        )
    )
    if not isinstance(response.content, HumanResponseText):
        raise ValueError("Expected a text response for the research note")
    note = (response.content.text or "").strip()
    if note == "__CANCEL__":
        return False, None
    if not note:
        raise ValueError("Shortlisting requires a non-empty grounded research note")
    return True, note


def _required_prompts(
    chosen: str, *, rules_decision: str, note_available: bool
) -> tuple[bool, bool]:
    """What the human must still be asked for. `(rationale, research_note)`.

    Pure, so the rule can be asserted without a model, an interaction manager or a
    running cluster — see `verify_approval_tokens.py`.

    The second element is the one worth stating. The MCP refuses a shortlist with
    no grounded note, and when the engine says `research` the model has no reason to
    have drafted one, so a human-initiated promotion used to be refused for a
    missing argument *the model* was supposed to supply. The person could only reach
    shortlist where the model had already been, which is permission rather than
    initiative — and initiative is the property this flow exists to provide.
    """
    needs_rationale = chosen != rules_decision
    needs_note = chosen == "shortlist" and not note_available
    return needs_rationale, needs_note


async def _decide(
    prompt_text: str, *, etf_id: str, rules_decision: str, note_available: bool
) -> tuple[bool, str | None, str | None, str | None]:
    """Let the user confirm the deterministic decision or choose a different one.

    Returns (approved, chosen_decision, override_rationale, research_note).

    The default is `rules_decision`, always. Whatever the model recommended is
    shown as advisory context above this prompt and does not move the default in
    either direction, so "Confirm" is only ever labelled on the engine's answer.

    This is where the person holds the initiative. Previously the only way to
    reach a different decision was for the *model* to propose one, which the human
    could then ratify: an override could not be started by a person, only accepted
    from a model. For an investment control that is the wrong direction of
    initiative, so every decision is offered here regardless of what the model
    proposed.

    All three are offered even where a hard constraint forbids one. The MCP is the
    authority on those, not this prompt, and it re-checks them after approval; a
    refusal there is the control demonstrating itself rather than a gap. Filtering
    here would duplicate policy into the UI layer and could imply the constraint
    lives in the prompt.
    """
    options = [
        MultipleChoiceOption(
            id=decision,
            value=decision,
            label=(
                f"Confirm — {decision}"
                if decision == rules_decision
                else f"Override — {decision}"
            ),
            description=_option_description(
                decision,
                rules_decision=rules_decision,
                note_available=note_available,
            ),
        )
        for decision in ("reject", "research", "shortlist")
    ]
    options.append(
        MultipleChoiceOption(
            id="cancel", value="__CANCEL__", label="Cancel", description="Change nothing."
        )
    )

    response = await Context.get().user_interaction_manager.prompt_user_input(
        HumanPromptRadio(text=prompt_text, options=options)
    )
    if not isinstance(response.content, HumanResponseRadio):
        raise ValueError("Expected a decision choice")
    chosen = (response.content.selected_option.value or "").strip()
    if chosen == "__CANCEL__":
        return False, None, None, None
    if chosen not in _RANKS:
        raise ValueError(f"Unknown decision choice: {chosen!r}")

    needs_rationale, needs_note = _required_prompts(
        chosen, rules_decision=rules_decision, note_available=note_available
    )

    # The rationale comes first because it explains the *decision*; the note
    # explains the *candidate*. They are persisted as separate records because they
    # answer different questions, so neither may stand in for the other. Each is
    # asked only when it is actually missing, so confirming a decision the model
    # already prepared a note for stays a single interaction.
    rationale = None
    if needs_rationale:
        approved, rationale = await _ask_rationale(chosen, rules_decision)
        if not approved:
            return False, None, None, None

    note = None
    if needs_note:
        approved, note = await _ask_research_note(etf_id)
        if not approved:
            return False, None, None, None

    return True, chosen, rationale, note


async def _confirm(prompt_text: str) -> bool:
    """Plain confirmation for actions that carry no decision choice."""
    interaction_manager = Context.get().user_interaction_manager
    response = await interaction_manager.prompt_user_input(
        HumanPromptBinary(
            text=prompt_text,
            options=[
                BinaryHumanPromptOption(id="confirm", label="Confirm", value=True),
                BinaryHumanPromptOption(id="cancel", label="Cancel", value=False),
            ],
        )
    )
    if not isinstance(response.content, HumanResponseBinary):
        raise ValueError("Expected a confirmation response")
    selected = response.content.selected_option
    return bool(selected is not None and selected.value is True)


def _prompt_text(lines: list[str], summary: str, *, choosing: bool) -> str:
    lines = [*lines, f"Summary: {summary}"]
    lines.append(
        "Confirm the deterministic decision, or choose a different one. Any change is an "
        "explicit human override and requires a rationale."
        if choosing
        else "Confirm this state-changing action before it is applied."
    )
    lines.append(
        "This records a research decision only. Nothing is bought, sold or held."
    )
    return "\n".join(lines)


async def _mint_and_apply(
    *,
    action: str,
    etf_id: str,
    ttl_seconds: int,
    rules_decision: str | None = None,
    llm_recommendation: str | None = None,
    requested_decision: str | None = None,
    override_requested: bool = False,
    override_rationale: str | None = None,
    assignee: str | None = None,
    research_note: str | None = None,
) -> str:
    """Mint the approval the human just gave and apply exactly that change.

    Separate from the prompting so each action can collect its own shape of
    confirmation exactly once. Folding the two together is how a caller ends up
    asking the same person to confirm twice.
    """
    actor_id, request_id = _identity()

    claims: dict[str, object] = {
        "v": 1,
        "exp": int(time.time()) + ttl_seconds,
        "action": action,
        "etf_id": etf_id,
        "actor_id": actor_id,
        "request_id": request_id,
        "rules_decision": rules_decision,
        "llm_recommendation": llm_recommendation,
        "requested_decision": requested_decision,
        "override_requested": override_requested,
        "assignee": assignee,
        "research_note_sha256": _research_note_hash(research_note),
        # The note travels inside the signed token and is persisted verbatim, so
        # the approved text is by construction the text that is stored.
        "research_note": research_note,
        "override_rationale": override_rationale,
        "nonce": str(uuid.uuid4()),
    }
    outcome = await _execute(_mint_token(_secret(), claims), request_id)
    return _approval_result(
        etf_id=etf_id,
        action=action,
        request_id=request_id,
        committed=bool(outcome.get("ok")),
        result=outcome.get("result"),
    )


async def _decide_then_apply(
    *,
    action: str,
    etf_id: str,
    prompt_lines: list[str],
    summary: str,
    ttl_seconds: int,
    rules_decision: str,
    llm_recommendation: str | None,
    research_note: str | None,
) -> str:
    """Offer every decision, then apply the one the human chose.

    The user picks the final decision, and `rules_decision` is the default they are
    confirming. An override is whatever differs from the *engine* — never from the
    model, whose recommendation is context. The MCP re-derives the same thing from
    the signed claims, so this side cannot get it wrong unnoticed.
    """
    approved, chosen, override_rationale, human_note = await _decide(
        _prompt_text(prompt_lines, summary, choosing=True),
        etf_id=etf_id,
        rules_decision=rules_decision,
        note_available=bool((research_note or "").strip()),
    )
    if not approved or chosen is None:
        return _cancelled(etf_id, action, "The human cancelled the requested change.")
    return await _mint_and_apply(
        action=action,
        etf_id=etf_id,
        ttl_seconds=ttl_seconds,
        rules_decision=rules_decision,
        llm_recommendation=llm_recommendation,
        requested_decision=chosen,
        override_requested=chosen != rules_decision,
        override_rationale=override_rationale,
        # The human's own note when they supplied one; the model's draft otherwise.
        # Either way it is the text that was on screen when they approved.
        research_note=human_note or research_note,
    )


async def _confirm_then_apply(
    *,
    action: str,
    etf_id: str,
    prompt_lines: list[str],
    summary: str,
    ttl_seconds: int,
    **claims: object,
) -> str:
    """Take a plain yes/no confirmation, then apply the change."""
    if not await _confirm(_prompt_text(prompt_lines, summary, choosing=False)):
        return _cancelled(etf_id, action, "The human cancelled the requested change.")
    return await _mint_and_apply(action=action, etf_id=etf_id, ttl_seconds=ttl_seconds, **claims)  # type: ignore[arg-type]


@register_function(config_type=CommitEvaluationConfig)
async def etf_commit_evaluation(config: CommitEvaluationConfig, builder: Builder):
    """Register the approval-gated initial review decision."""

    del builder
    _secret()

    async def commit_evaluation(request: CommitEvaluationRequest) -> str:
        """Commit the initial review decision for an ETF after explicit human confirmation.

        Only an UNREVIEWED ETF can take an initial decision, and it is valid exactly
        once. Check the review state with get_etf first. To move an ETF that is
        already in research onto the shortlist, use shortlist_etf.

        Call evaluate_etf first and pass its decision as rules_decision. That
        decision is the default the user is asked to confirm. Your
        llm_recommendation is advisory context: it may equal rules_decision or be
        more conservative, never more optimistic, and it does not change the
        default in either direction. Set requested_decision to the user's explicit
        choice when they want something other than rules_decision, in which case
        they must type a rationale. If the final decision is shortlist, call
        get_research_context and pass a grounded research_note. The change is
        applied once the human confirms.

        This records a research decision. It does not buy, sell or hold anything.
        """
        if (
            request.llm_recommendation is not None
            and _RANKS[request.llm_recommendation] > _RANKS[request.rules_decision]
        ):
            raise ValueError(
                "A model recommendation may never be more optimistic than the deterministic "
                "decision. Put the user's explicit choice in requested_decision instead."
            )

        if request.requested_decision == "shortlist" and not (request.research_note or "").strip():
            raise ValueError(
                "Shortlisting requires research_note. Call get_research_context first and "
                "pass the grounded note text."
            )

        # The three roles are labelled separately in the prompt, because a person
        # authorising an investment decision has to be able to see who is saying
        # what. Collapsing the engine and the model into one "system decision" is
        # how the model's opinion ends up being ratified as policy.
        lines = [
            f"ETF: {request.etf_id}",
            "Action: commit review decision",
            f"Deterministic engine (authoritative): {request.rules_decision}",
        ]
        if request.llm_recommendation:
            lines.append(
                f"Model recommendation (advisory only): {request.llm_recommendation}"
            )
        lines.append(f"Default decision: {request.rules_decision}")
        if request.requested_decision != request.rules_decision:
            lines.append(
                f"Requested by the user, as an override: {request.requested_decision}"
            )
        if request.research_note:
            lines.append("Research note to persist:")
            lines.append(request.research_note)

        return await _decide_then_apply(
            action="commit",
            etf_id=request.etf_id,
            prompt_lines=lines,
            summary=request.summary,
            ttl_seconds=config.token_ttl_seconds,
            rules_decision=request.rules_decision,
            llm_recommendation=request.llm_recommendation,
            research_note=request.research_note,
        )

    yield FunctionInfo.from_fn(commit_evaluation, description=commit_evaluation.__doc__)


@register_function(config_type=ShortlistEtfConfig)
async def etf_shortlist_etf(config: ShortlistEtfConfig, builder: Builder):
    """Register the approval-gated shortlist action."""

    del builder
    _secret()

    async def shortlist_etf(request: ShortlistEtfRequest) -> str:
        """Add an ETF to the shortlist after explicit human confirmation.

        Available on an UNREVIEWED or RESEARCH ETF. One that is already
        SHORTLISTED, ASSIGNED or REJECTED cannot be shortlisted again.

        Call evaluate_etf for rules_decision and get_research_context to draft the
        grounded research_note. When the deterministic decision is only research,
        shortlisting is a promotion: the user is asked for a rationale, which is
        recorded. A non-bypassable hard constraint can never be shortlisted past,
        whatever the user asks for.

        Shortlisting records an investment candidate for follow-up. It places no
        order and opens no position.
        """
        lines = [
            f"ETF: {request.etf_id}",
            "Action: shortlist",
            f"Deterministic engine (authoritative): {request.rules_decision}",
            "Requested decision: shortlist",
            "Research note to persist:",
            request.research_note,
        ]
        # Shortlisting above the deterministic decision is a promotion. Only a
        # human may make one, and the rationale is collected *instead of* a plain
        # confirmation rather than in addition to it: a required free-text answer
        # already is the confirmation, and asking twice trains people to click
        # through prompts.
        promotion = _RANKS["shortlist"] > _RANKS[request.rules_decision]
        if promotion:
            lines.append(f"Summary: {request.summary}")
            approved, rationale = await _ask_rationale(
                "shortlist", request.rules_decision, context=lines
            )
            if not approved:
                return _cancelled(
                    request.etf_id, "shortlist", "The human cancelled the requested change."
                )
            return await _mint_and_apply(
                action="shortlist",
                etf_id=request.etf_id,
                ttl_seconds=config.token_ttl_seconds,
                rules_decision=request.rules_decision,
                requested_decision="shortlist",
                override_requested=True,
                override_rationale=rationale,
                research_note=request.research_note,
            )

        return await _confirm_then_apply(
            action="shortlist",
            etf_id=request.etf_id,
            prompt_lines=lines,
            summary=request.summary,
            ttl_seconds=config.token_ttl_seconds,
            rules_decision=request.rules_decision,
            requested_decision="shortlist",
            research_note=request.research_note,
        )

    yield FunctionInfo.from_fn(shortlist_etf, description=shortlist_etf.__doc__)


@register_function(config_type=AssignEtfConfig)
async def etf_assign_etf(config: AssignEtfConfig, builder: Builder):
    """Register the approval-gated research-owner assignment."""

    del builder
    _secret()

    async def assign_etf(request: AssignEtfRequest) -> str:
        """Assign a research owner to an ETF after explicit human confirmation.

        Available on an ETF in RESEARCH, SHORTLISTED or ASSIGNED state;
        reassignment is allowed. Unreviewed and rejected ETFs are not assignable. A
        shortlisted ETF must already carry a grounded research note.

        Assignment means this person owns the next research decision. It is not an
        instruction to trade and nothing is executed.
        """
        lines = [
            f"ETF: {request.etf_id}",
            "Action: assign a research owner",
            f"Research owner: {request.assignee}",
        ]
        return await _confirm_then_apply(
            action="assign",
            etf_id=request.etf_id,
            prompt_lines=lines,
            summary=request.summary,
            ttl_seconds=config.token_ttl_seconds,
            assignee=request.assignee,
        )

    yield FunctionInfo.from_fn(assign_etf, description=assign_etf.__doc__)
