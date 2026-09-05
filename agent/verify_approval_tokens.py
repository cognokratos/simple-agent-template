#!/usr/bin/env python3
"""Direct enforcement tests for the human-approval boundary on the MCP server.

Every state change in this system is gated by an HMAC-signed, payload-bound,
single-use approval token. Those checks are the control the architecture rests
on, and the live evaluation suites all assert that *no* mutation occurred, so
nothing else executes the mutation path automatically.

This suite drives `POST /approvals/execute` on the Rust MCP server directly. No
LLM, no agent, no UI, so it is fast and fully deterministic. It mints tokens with
the *production* signer imported from `nat_streaming_react.approval`, not a copy,
so a change to the token format breaks this suite rather than silently diverging
from it.

Run it with `make verify-approvals`, which resets the dedicated test ETFs first so
the suite is idempotent. History rows deliberately accumulate: `audit_events` is
append-only at the database level and must not be cleaned up.

The ETFs used here are chosen to avoid every ETF referenced by an evaluation
dataset or the demo guide, so running this never disturbs a demo.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

# The production signer. Importing it is deliberate: these tests must fail if the
# token format changes, rather than testing a stale reimplementation.
from nat_streaming_react.approval import (
    _approval_result,
    _mint_token,
    _required_prompts,
    _research_note_hash,
    _secret,
)

# Dedicated test ETFs, disjoint from evaluation/datasets/*.json and docs/DEMO.md.
# Their deterministic decisions come from the shipped engine and are pinned in
# evaluation/results/deterministic-etf-baseline.json.
RESEARCH_FUND = "VJPN-LSE"  # deterministic decision: research (score 72)
SCRATCH = "IH2O-LSE"  # deterministic decision: research; never mutated
REJECT_FUND = "XNIF-XETRA"  # deterministic decision: reject (score 38)
NON_UCITS = "QQQ-NASDAQ"  # hard-constraint reject, whatever the score
SHORTLIST_FUND = "VHYL-LSE"  # deterministic decision: shortlist (score 77)
PROMOTE_FUND = "EQQQ-LSE"  # research; used for a human promotion to shortlist
OVERRIDE_FUND = "CW8-EPA"  # research; used for a human downgrade to reject
ASSIGN_FUND = "VFEM-LSE"  # research; used for the assignment path
# The deterministic engine shortlists both of these. Reserved for the
# decision-authority cases, where a model recommends something more conservative
# and the question is whether the *engine's* decision is still the default.
#
# Unlike the funds above they do appear in a read-only labelled case, which asserts
# the deterministic decision and score only. Those are a pure function of the ETF
# facts, the rules and the profile — never of workflow state — so committing a
# decision here cannot affect that assertion. Neither appears in docs/DEMO.md or in
# an injection payload.
ADVISORY_FUND = "VWRL-LSE"  # deterministic decision: shortlist (score 82)
ADVISORY_OVERRIDE_FUND = "IUSN-XETRA"  # shortlist (score 75); downgraded to research


def research_note_for(etf_id: str) -> str:
    """A grounded, deliberately unembellished research note.

    Content is irrelevant to these assertions — only its binding to the token is —
    but it stays honest about asserting nothing the snapshot does not contain, and
    about what a shortlist does and does not mean.
    """
    return (
        f"{etf_id} was evaluated by the deterministic engine against the configured "
        "investor profile. No return forecast is implied and no position is held."
    )


def execute_url() -> str:
    mcp_url = os.environ["ETF_MCP_URL"]
    return mcp_url.rstrip("/").removesuffix("/mcp") + "/approvals/execute"


def claims(
    *,
    action: str,
    etf_id: str,
    request_id: str,
    rules_decision: str | None = None,
    llm_recommendation: str | None = None,
    requested_decision: str | None = None,
    override_requested: bool = False,
    override_rationale: str | None = None,
    assignee: str | None = None,
    research_note: str | None = None,
    ttl_seconds: int = 600,
    research_note_sha256: str | None = ...,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Build a claim set in exactly the shape `approval.py` mints."""
    return {
        "v": 1,
        "exp": int(time.time()) + ttl_seconds,
        "action": action,
        "etf_id": etf_id,
        "actor_id": "test-researcher",
        "request_id": request_id,
        "rules_decision": rules_decision,
        "llm_recommendation": llm_recommendation,
        "requested_decision": requested_decision,
        "override_requested": override_requested,
        "assignee": assignee,
        # `...` means "derive it honestly"; an explicit value lets a test forge a
        # mismatch between the note and the hash it was signed under.
        "research_note_sha256": (
            _research_note_hash(research_note)
            if research_note_sha256 is ...
            else research_note_sha256
        ),
        "research_note": research_note,
        "override_rationale": override_rationale,
        "nonce": str(uuid.uuid4()),
    }


def post(token: str, request_id: str) -> tuple[int, dict[str, Any]]:
    payload = json.dumps({"approval_token": token, "request_id": request_id}).encode()
    request = urllib.request.Request(
        execute_url(),
        data=payload,
        method="POST",
        headers={
            "content-type": "application/json",
            "Authorization": f"Bearer {os.environ['MCP_API_KEY']}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        try:
            return error.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return error.code, {"raw": raw}


def message(body: dict[str, Any]) -> str:
    """The MCP reports refusals two ways; normalise them.

    A rejected mutation is a `CallToolResult::error`, surfacing as HTTP 200 with
    `ok: false` and the reason in `result`. A hard `McpError` (a replayed nonce,
    for instance) surfaces as HTTP 409 with the reason in `error`.
    """
    for key in ("error", "result"):
        value = body.get(key)
        if isinstance(value, str):
            return value
    return json.dumps(body)


@dataclass
class Case:
    name: str
    why: str
    expect_ok: bool
    expect_status: int = 200
    expect_message: str = ""
    expect_state: str | None = None
    #: Exact field values required on a successful mutation result. A dotted key
    #: reads a nested field, so an assertion can name `committed_snapshot.decision`
    #: without the result having to flatten it back into an ambiguous top-level one.
    expect_result: dict[str, Any] = field(default_factory=dict)
    #: Field names that must be absent from a successful result.
    expect_absent: tuple[str, ...] = ()
    passed: bool = False
    detail: str = ""


CASES: list[Case] = []

_MISSING = object()


def _at(result: dict[str, Any], path: str) -> Any:
    value: Any = result
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def check(case: Case, status: int, body: dict[str, Any]) -> None:
    ok = body.get("ok") is True
    text = message(body)
    failures: list[str] = []

    if status != case.expect_status:
        failures.append(f"HTTP {status}, expected {case.expect_status}")
    if ok != case.expect_ok:
        failures.append(f"ok={ok}, expected {case.expect_ok}")
    if case.expect_message and case.expect_message.lower() not in text.lower():
        failures.append(f"message {text!r} does not contain {case.expect_message!r}")
    result = body.get("result")
    result = result if isinstance(result, dict) else {}
    if case.expect_state is not None and result.get("new_state") != case.expect_state:
        failures.append(f"new_state={result.get('new_state')!r}, expected {case.expect_state!r}")
    for key, expected in case.expect_result.items():
        observed = _at(result, key)
        if observed is _MISSING:
            failures.append(f"{key} is absent, expected {expected!r}")
        elif observed != expected:
            failures.append(f"{key}={observed!r}, expected {expected!r}")
    for key in case.expect_absent:
        if _at(result, key) is not _MISSING:
            failures.append(f"{key} is present and must not be")

    case.passed = not failures
    case.detail = "; ".join(failures) if failures else text[:110]
    CASES.append(case)


def run(case: Case, claim_set: dict[str, Any], *, request_id: str, mangle: str = "") -> str:
    token = _mint_token(_secret(), claim_set)
    if mangle == "signature":
        payload_b64, signature = token.split(".")
        flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
        token = f"{payload_b64}.{flipped}"
    status, body = post(token, request_id)
    check(case, status, body)
    return token


def check_refusal_is_reported_as_refusal() -> None:
    """A refused change must never be described to the user as applied.

    The MCP re-checks hard constraints after the human approves, so approval and
    outcome are different things. The cases below prove the MCP refuses; this
    proves the agent is told the truth about it.
    """
    refused = json.loads(
        _approval_result(
            etf_id="X",
            action="commit",
            request_id="r",
            committed=False,
            result="Hard constraint HC-UCITS",
        )
    )
    applied = json.loads(
        _approval_result(
            etf_id="X",
            action="commit",
            request_id="r",
            committed=True,
            result={"new_state": "RESEARCH"},
        )
    )
    failures = []
    if refused["committed"] is not False:
        failures.append("refused payload does not report committed=false")
    if "already applied" in refused["next_step"]:
        failures.append("a refusal still tells the model the change was applied")
    if "REFUSED" not in refused["next_step"]:
        failures.append("a refusal does not tell the model it was refused")
    if "already applied" not in applied["next_step"]:
        failures.append("a commit no longer tells the model the change was applied")
    if "bought, sold or held" not in applied["next_step"]:
        failures.append("a commit no longer states that nothing was traded")
    CASES.append(
        Case(
            "refusal is reported as a refusal",
            "an approved-but-refused change must not be described as applied",
            expect_ok=False,
            passed=not failures,
            detail="; ".join(failures) if failures else "committed=false yields a refusal instruction",
        )
    )


def check_a_human_promotion_can_be_completed_without_the_model() -> None:
    """A person choosing shortlist must be asked for everything shortlist needs.

    `make verify-hitl` drives this through a live model, so what it observes depends
    on whether the model happened to draft a note. This asserts the rule itself,
    deterministically: the flow must never reach the MCP missing an argument nobody
    was asked for, because the MCP would refuse the human's own promotion for a
    field *the model* omitted.
    """
    failures: list[str] = []
    cases = [
        # (chosen, rules_decision, note_available) -> (rationale, note)
        (("shortlist", "research", False), (True, True), "a human promotion with no drafted note"),
        (("shortlist", "research", True), (True, False), "a human promotion, note already drafted"),
        (("shortlist", "shortlist", False), (False, True), "confirming a shortlist with no note"),
        (("shortlist", "shortlist", True), (False, False), "confirming a shortlist with a note"),
        (("reject", "research", False), (True, False), "a downgrade needs no research note"),
        (("research", "research", False), (False, False), "confirming the engine asks nothing"),
    ]
    for (chosen, rules_decision, note_available), expected, why in cases:
        observed = _required_prompts(
            chosen, rules_decision=rules_decision, note_available=note_available
        )
        if observed != expected:
            failures.append(f"{why}: expected {expected}, got {observed}")

    CASES.append(
        Case(
            "a human promotion is completable without the model",
            "the person is asked for every field their own choice requires",
            expect_ok=True,
            passed=not failures,
            detail="; ".join(failures) if failures else f"{len(cases)} prompt plans correct",
        )
    )


def main() -> int:
    print(f"Approval-boundary enforcement suite -> {execute_url()}\n")
    check_refusal_is_reported_as_refusal()
    check_a_human_promotion_can_be_completed_without_the_model()

    # ---------------------------------------------------------------- rejections
    # None of these may change any state. Each isolates exactly one control.

    request_id = str(uuid.uuid4())
    run(
        Case(
            "forged signature",
            "an attacker-supplied token must not verify",
            expect_ok=False,
            expect_status=400,
        ),
        claims(
            action="commit",
            etf_id=SCRATCH,
            request_id=request_id,
            rules_decision="research",
            requested_decision="research",
        ),
        request_id=request_id,
        mangle="signature",
    )

    run(
        Case(
            "expired token",
            "an approval must not be usable indefinitely",
            expect_ok=False,
            expect_status=400,
        ),
        claims(
            action="commit",
            etf_id=SCRATCH,
            request_id=request_id,
            rules_decision="research",
            requested_decision="research",
            ttl_seconds=-60,
        ),
        request_id=request_id,
    )

    run(
        Case(
            "request_id mismatch",
            "an approval is bound to the authenticated request that produced it",
            expect_ok=False,
            expect_message="not bound to this authenticated request",
        ),
        claims(
            action="commit",
            etf_id=SCRATCH,
            request_id="request-the-token-was-minted-for",
            rules_decision="research",
            requested_decision="research",
        ),
        request_id="a-different-request",
    )

    run(
        Case(
            "token ETF is compared to the row that was locked",
            "the tool must re-verify the binding rather than trust the token's own field",
            expect_ok=False,
            expect_message="not bound to this action and ETF",
        ),
        # The claim resolves to a real row after trimming, so the mutation reaches
        # the verifier — and the verifier compares the *untrimmed* claim against
        # the identifier the row was locked under. Accepting it would mean the
        # token's own etf_id was taken on trust, which is the whole point of
        # binding it.
        {
            **claims(
                action="commit",
                etf_id=SCRATCH,
                request_id=request_id,
                rules_decision="research",
                requested_decision="research",
            ),
            "etf_id": RESEARCH_FUND + " ",
        },
        request_id=request_id,
    )

    run(
        Case(
            "research note does not match its own hash",
            "the approved note cannot be swapped after signing",
            expect_ok=False,
            expect_message="research note does not match its own signature",
        ),
        claims(
            action="commit",
            etf_id=RESEARCH_FUND,
            request_id=request_id,
            rules_decision="research",
            requested_decision="research",
            research_note=research_note_for(RESEARCH_FUND),
            research_note_sha256=_research_note_hash("a note the human never saw"),
        ),
        request_id=request_id,
    )

    run(
        Case(
            "stale deterministic result",
            "a token is void if the deterministic decision changed under it",
            expect_ok=False,
            expect_message="different deterministic decision",
        ),
        claims(
            action="commit",
            etf_id=SCRATCH,  # really research
            request_id=request_id,
            rules_decision="shortlist",
            requested_decision="shortlist",
        ),
        request_id=request_id,
    )

    run(
        Case(
            "model promotion inside a signed token",
            "the policy is re-enforced below the model",
            expect_ok=False,
            expect_message="more optimistic",
        ),
        claims(
            action="commit",
            etf_id=RESEARCH_FUND,
            request_id=request_id,
            rules_decision="research",
            llm_recommendation="shortlist",
            requested_decision="shortlist",
        ),
        request_id=request_id,
    )

    run(
        Case(
            "override without rationale",
            "a human override in either direction requires a rationale",
            expect_ok=False,
            expect_message="non-empty rationale",
        ),
        claims(
            action="commit",
            etf_id=RESEARCH_FUND,
            request_id=request_id,
            rules_decision="research",
            requested_decision="reject",
            override_requested=True,
            override_rationale="   ",
        ),
        request_id=request_id,
    )

    run(
        Case(
            "override flag disagrees with the decision",
            "an override cannot be smuggled in either direction",
            expect_ok=False,
            expect_message="override flag does not match",
        ),
        claims(
            action="commit",
            etf_id=RESEARCH_FUND,
            request_id=request_id,
            rules_decision="research",
            requested_decision="research",  # equals the system decision
            override_requested=True,  # ...yet claims to be an override
            override_rationale="claims to override without changing anything",
        ),
        request_id=request_id,
    )

    run(
        Case(
            "shortlist a non-UCITS fund with a VALID human token",
            "a non-bypassable hard constraint survives human approval",
            expect_ok=False,
            expect_message="HC-UCITS",
        ),
        claims(
            action="commit",
            etf_id=NON_UCITS,
            request_id=request_id,
            rules_decision="reject",
            requested_decision="shortlist",
            override_requested=True,
            override_rationale="the investor wants this fund regardless of domicile",
            research_note=research_note_for(NON_UCITS),
        ),
        request_id=request_id,
    )

    run(
        Case(
            "research a non-UCITS fund with a VALID human token",
            "the constraint blocks every decision above reject, not just shortlist",
            expect_ok=False,
            expect_message="HC-UCITS",
        ),
        claims(
            action="commit",
            etf_id=NON_UCITS,
            request_id=request_id,
            rules_decision="reject",
            requested_decision="research",
            override_requested=True,
            override_rationale="the investor wants to keep looking at it",
        ),
        request_id=request_id,
    )

    run(
        Case(
            "shortlist_etf on a non-UCITS fund",
            "the hard constraint is enforced on every mutation path, not just commit",
            expect_ok=False,
            expect_message="HC-UCITS",
        ),
        claims(
            action="shortlist",
            etf_id=NON_UCITS,
            request_id=request_id,
            rules_decision="reject",
            requested_decision="shortlist",
            override_requested=True,
            override_rationale="the investor insists",
            research_note=research_note_for(NON_UCITS),
        ),
        request_id=request_id,
    )

    run(
        Case(
            "shortlist without a research note",
            "an investment candidate must carry its reasoning",
            expect_ok=False,
            expect_message="research note",
        ),
        claims(
            action="commit",
            etf_id=RESEARCH_FUND,
            request_id=request_id,
            rules_decision="research",
            requested_decision="shortlist",
            override_requested=True,
            override_rationale="looks attractive to me",
        ),
        request_id=request_id,
    )

    run(
        Case(
            "assign an UNREVIEWED ETF",
            "assignment requires a decided candidate; reassignment of an ASSIGNED one is allowed",
            expect_ok=False,
            expect_message="cannot be assigned",
        ),
        claims(
            action="assign",
            etf_id=SCRATCH,
            request_id=request_id,
            assignee="victor",
        ),
        request_id=request_id,
    )

    # ------------------------------------------------------------- happy path #1
    # A valid token commits exactly once, and only once.

    commit_request = str(uuid.uuid4())
    run(
        Case(
            "valid reject commits",
            "the approved decision is applied verbatim",
            expect_ok=True,
            expect_state="REJECTED",
            expect_result={"final_decision": "reject", "override_applied": False},
        ),
        claims(
            action="commit",
            etf_id=REJECT_FUND,
            request_id=commit_request,
            rules_decision="reject",
            requested_decision="reject",
        ),
        request_id=commit_request,
    )

    # Regression test for the state-machine gap this suite originally exposed: the
    # initial decision used to have no review-state guard, so a decided candidate
    # could be re-decided and recorded as a meaningless REJECTED -> REJECTED
    # transition.
    redecide_request = str(uuid.uuid4())
    run(
        Case(
            "re-decide an already-REJECTED ETF",
            "a decided candidate cannot be silently re-decided",
            expect_ok=False,
            expect_message="Only unreviewed ETFs",
        ),
        claims(
            action="commit",
            etf_id=REJECT_FUND,
            request_id=redecide_request,
            rules_decision="reject",
            requested_decision="reject",
        ),
        request_id=redecide_request,
    )

    assign_rejected_request = str(uuid.uuid4())
    run(
        Case(
            "assign a REJECTED ETF",
            "a rejected candidate is finished; nobody owns follow-up research on it",
            expect_ok=False,
            expect_message="cannot be assigned",
        ),
        claims(
            action="assign",
            etf_id=REJECT_FUND,
            request_id=assign_rejected_request,
            assignee="victor",
        ),
        request_id=assign_rejected_request,
    )

    # ------------------------------------------------------------- happy path #2
    # A human override may go in either direction provided it is justified and
    # logged. This is the downward one, which is the conservative case, so it has
    # to be proven to work rather than only proven to be blocked.
    #
    # It doubles as the control for the hard-constraint cases above. Those use the
    # same claim shape and differ only in the fund, so this passing is what makes
    # their rejection attributable to the constraint rather than to a malformed
    # override claim.

    override_request = str(uuid.uuid4())
    run(
        Case(
            "human downgrade override commits, with rationale",
            "an override in either direction is permitted when justified and logged",
            expect_ok=True,
            expect_state="REJECTED",
            expect_result={
                "rules_decision": "research",
                "llm_recommendation": None,
                "default_decision": "research",
                "human_override_decision": "reject",
                "final_decision": "reject",
                "override_applied": True,
                "override_rationale": "Swap-based replication is outside what this investor will hold.",
                "actor_id": "test-researcher",
            },
            # `system_decision` used to be `llm_recommendation or rules_decision`,
            # which made a conservative model recommendation the default. The field
            # is gone; `default_decision` is the engine's, always.
            expect_absent=("system_decision",),
        ),
        claims(
            action="commit",
            etf_id=OVERRIDE_FUND,
            request_id=override_request,
            rules_decision="research",
            requested_decision="reject",
            override_requested=True,
            override_rationale="Swap-based replication is outside what this investor will hold.",
        ),
        request_id=override_request,
    )

    # -------------------------------------------------- decision authority (A-E)
    # The deterministic engine is the default, and a model recommendation is
    # advisory in *both* directions. The regression these cover: the default used
    # to be `llm_recommendation or rules_decision`, so with the engine at shortlist
    # and the model at research, a person choosing the engine's own answer was
    # recorded as overriding the system — and choosing the model's was recorded as
    # agreeing with it. Exactly backwards.
    #
    # Case C (a more optimistic model proposal) is already covered above by "model
    # promotion inside a signed token". Case E (a non-bypassable constraint) is
    # covered by the three HC-UCITS cases. Both remain listed here for the reader.

    advisory_request = str(uuid.uuid4())
    run(
        Case(
            "A: engine shortlist, model research, human takes the engine's decision",
            "a conservative model recommendation is not the default, so agreeing with the "
            "engine is not an override",
            expect_ok=True,
            expect_state="SHORTLISTED",
            expect_result={
                "rules_decision": "shortlist",
                "llm_recommendation": "research",
                "default_decision": "shortlist",
                "final_decision": "shortlist",
                "override_applied": False,
                "human_override_decision": None,
                "override_rationale": None,
            },
            expect_absent=("system_decision",),
        ),
        claims(
            action="commit",
            etf_id=ADVISORY_FUND,
            request_id=advisory_request,
            rules_decision="shortlist",
            llm_recommendation="research",
            requested_decision="shortlist",
            research_note=research_note_for(ADVISORY_FUND),
        ),
        request_id=advisory_request,
    )

    # ...and the same claim set asserting an override is refused, so the flag
    # cannot be talked into existence by a conservative recommendation.
    false_override_request = str(uuid.uuid4())
    run(
        Case(
            "A': claiming an override while taking the engine's decision",
            "an unchanged decision is never an override, whatever the model said",
            expect_ok=False,
            expect_message="override flag does not match",
        ),
        claims(
            action="commit",
            etf_id=ADVISORY_OVERRIDE_FUND,
            request_id=false_override_request,
            rules_decision="shortlist",
            llm_recommendation="research",
            requested_decision="shortlist",
            override_requested=True,
            override_rationale="claims to override while agreeing with the engine",
            research_note=research_note_for(ADVISORY_OVERRIDE_FUND),
        ),
        request_id=false_override_request,
    )

    advisory_override_request = str(uuid.uuid4())
    run(
        Case(
            "B: engine shortlist, model research, human follows the model",
            "moving away from the deterministic decision is a human override and needs a "
            "rationale, even when a model suggested it first",
            expect_ok=True,
            expect_state="RESEARCH",
            expect_result={
                "rules_decision": "shortlist",
                "llm_recommendation": "research",
                "default_decision": "shortlist",
                "human_override_decision": "research",
                "final_decision": "research",
                "override_applied": True,
                "override_rationale": "Taking the model's caution on concentration risk.",
            },
        ),
        claims(
            action="commit",
            etf_id=ADVISORY_OVERRIDE_FUND,
            request_id=advisory_override_request,
            rules_decision="shortlist",
            llm_recommendation="research",
            requested_decision="research",
            override_requested=True,
            override_rationale="Taking the model's caution on concentration risk.",
        ),
        request_id=advisory_override_request,
    )

    # ------------------------------------------------------------- happy path #3
    # Shortlisting straight from UNREVIEWED, where the engine already says
    # shortlist, so it is a confirmation rather than a promotion.

    shortlist_request = str(uuid.uuid4())
    run(
        Case(
            "valid shortlist commits from UNREVIEWED",
            "confirming the deterministic decision needs no override",
            expect_ok=True,
            expect_state="SHORTLISTED",
            expect_result={
                "final_decision": "shortlist",
                "override_applied": False,
                "actor_id": "test-researcher",
            },
        ),
        claims(
            action="shortlist",
            etf_id=SHORTLIST_FUND,
            request_id=shortlist_request,
            rules_decision="shortlist",
            requested_decision="shortlist",
            research_note=research_note_for(SHORTLIST_FUND),
        ),
        request_id=shortlist_request,
    )

    reshortlist_request = str(uuid.uuid4())
    run(
        Case(
            "shortlist an already-SHORTLISTED ETF",
            "shortlisting is not a path back into a decided candidate",
            expect_ok=False,
            expect_message="Only unreviewed or in-research ETFs can be shortlisted",
        ),
        claims(
            action="shortlist",
            etf_id=SHORTLIST_FUND,
            request_id=reshortlist_request,
            rules_decision="shortlist",
            requested_decision="shortlist",
            research_note=research_note_for(SHORTLIST_FUND),
        ),
        request_id=reshortlist_request,
    )

    # ------------------------------------------------------------- happy path #4
    # A human promotion: the engine says research, the person shortlists anyway
    # with a rationale. This is the direction the model may never take on its own.

    promote_request = str(uuid.uuid4())
    run(
        Case(
            "promotion without the override flag is refused",
            "shortlisting above the deterministic decision must declare itself",
            expect_ok=False,
            expect_message="override flag does not match",
        ),
        claims(
            action="shortlist",
            etf_id=PROMOTE_FUND,
            request_id=promote_request,
            rules_decision="research",
            requested_decision="shortlist",
            research_note=research_note_for(PROMOTE_FUND),
        ),
        request_id=promote_request,
    )

    promote_request = str(uuid.uuid4())
    run(
        Case(
            "human promotion to shortlist commits, with rationale",
            "a person may shortlist above the engine; a model may not",
            expect_ok=True,
            expect_state="SHORTLISTED",
            expect_result={
                "final_decision": "shortlist",
                "override_applied": True,
                "override_rationale": "Accepting the concentration for a satellite position.",
                "actor_id": "test-researcher",
            },
        ),
        claims(
            action="shortlist",
            etf_id=PROMOTE_FUND,
            request_id=promote_request,
            rules_decision="research",
            requested_decision="shortlist",
            override_requested=True,
            override_rationale="Accepting the concentration for a satellite position.",
            research_note=research_note_for(PROMOTE_FUND),
        ),
        request_id=promote_request,
    )

    # ------------------------------------------------------------- happy path #5
    # Decide, then assign, so the assign-specific checks run against a real
    # in-research candidate rather than being short-circuited by the state guard.

    decide_request = str(uuid.uuid4())
    run(
        Case(
            "valid research decision commits",
            "sets up the assignment path",
            expect_ok=True,
            expect_state="RESEARCH",
        ),
        claims(
            action="commit",
            etf_id=ASSIGN_FUND,
            request_id=decide_request,
            rules_decision="research",
            requested_decision="research",
        ),
        request_id=decide_request,
    )

    assign_request = str(uuid.uuid4())
    run(
        Case(
            "assign token carrying a decision",
            "an assign approval must not smuggle a review decision",
            expect_ok=False,
            expect_message="must not carry a decision",
        ),
        claims(
            action="assign",
            etf_id=ASSIGN_FUND,
            request_id=assign_request,
            rules_decision="research",
            assignee="victor",
        ),
        request_id=assign_request,
    )

    assigned_token = run(
        Case(
            "valid assignment commits",
            "the approved research owner is applied and the decision is unchanged",
            expect_ok=True,
            expect_state="ASSIGNED",
            expect_result={
                "assignee": "victor",
                "actor_id": "test-researcher",
                # The committed decision and the current evaluation are separate
                # objects, each naming its own policy versions. A flat `decision`
                # field here could not say which generation it belonged to.
                "committed_snapshot.decision": "research",
                "current_evaluation.decision": "research",
            },
            expect_absent=("decision", "investment_score", "rules_decision"),
        ),
        claims(
            action="assign",
            etf_id=ASSIGN_FUND,
            request_id=assign_request,
            assignee="victor",
        ),
        request_id=assign_request,
    )

    reassign_request = str(uuid.uuid4())
    run(
        Case(
            "reassignment updates the research owner",
            "an ASSIGNED candidate can change hands without changing its decision",
            expect_ok=True,
            expect_state="ASSIGNED",
            expect_result={"assignee": "ada", "committed_snapshot.decision": "research"},
        ),
        claims(
            action="assign",
            etf_id=ASSIGN_FUND,
            request_id=reassign_request,
            assignee="ada",
        ),
        request_id=reassign_request,
    )

    # Replay is tested on assign because reassignment stays legal by design, so the
    # nonce is the only thing standing between a replayed token and a second
    # mutation. On the initial decision the state guard would mask it.
    status, body = post(assigned_token, assign_request)
    check(
        Case(
            "replayed token",
            "a nonce is consumed exactly once, atomically with the mutation",
            expect_ok=False,
            expect_status=409,
            expect_message="already been consumed",
        ),
        status,
        body,
    )

    # Cross-action reuse — spending an assign approval as a shortlist — is not
    # reachable through this endpoint by construction: the dispatcher routes on the
    # action inside the signature, so a caller cannot point a token at a different
    # tool. The tool-level check that each tool re-verifies its own action is
    # covered by `approval::tests::a_token_for_another_action_or_etf_is_rejected`
    # in the Rust crate, which exercises the verifier directly.

    # ------------------------------------------------------------------- report
    width = max(len(case.name) for case in CASES)
    for case in CASES:
        mark = "PASS" if case.passed else "FAIL"
        print(f"  [{mark}] {case.name.ljust(width)}  {case.detail}")

    failed = [case for case in CASES if not case.passed]
    print(f"\n{len(CASES) - len(failed)}/{len(CASES)} approval-boundary assertions passed")
    if failed:
        print("\nFailed:")
        for case in failed:
            print(f"  - {case.name}: {case.why}\n      {case.detail}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
