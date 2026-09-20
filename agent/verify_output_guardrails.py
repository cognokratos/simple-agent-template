"""Regression checks for the streamed ticket-triage output guardrails.

Covers both configured output rails and what each one can actually do:

* ``regex check output``            — blocks credential-shaped values, on a
                                      rolling window of chunks, at full
                                      streaming speed.
* ``mask sensitive data on output`` — masks the configured PII entities. NeMo's
                                      streaming rail runner can only use an
                                      action's return value to decide
                                      block/no-block, never to rewrite text, so
                                      masking cannot run token-by-token at all.
                                      When this flow is configured, the
                                      middleware buffers the complete answer
                                      and masks it once
                                      (``_stream_with_buffered_masking``)
                                      instead of releasing chunks unmasked;
                                      see ``check_streaming_pii_protection``
                                      below for the behavioral proof.

Run inside the agent image::

    docker compose exec agent python /app/verify_output_guardrails.py
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
from pathlib import Path

import yaml
from nemoguardrails.rails.llm.options import GenerationLogOptions
from nemoguardrails.rails.llm.options import GenerationOptions

from nat_streaming_react.guardrails_compat import mask_sensitive_data
from nat_streaming_react.guardrails_compat import masking_is_available
from nat_streaming_react.guardrails_compat import register_rail_compatibility
from nat_streaming_react.guardrails_compat import RailFlowParameterGuard
from nat_streaming_react.guardrails_compat import RailsPool
from nat_streaming_react.text_guardrails import TextGuardrailsMiddleware

CONFIG_PATH = Path(__file__).with_name("config.yml")

# Exactly the shape of a real answer. Every field must survive the output rails:
# ticket ids, event ids and amounts are the evidence a triage decision rests
# on, and a rail that eats them makes the system useless while looking like it
# is working.
TICKET_OUTPUT = """\
1. **TKT-1004** — Duplicate charge
   - **Status:** open
   - **Priority:** high
   - **History events:** 3, totalling $172.80
   - **Largest:** EVT-1301 at $86.40 on 2026-09-16
   - **Escalated:** true
"""

# The PII the masking rail is configured to remove. Deliberately includes the
# entity types listed in config.yml and nothing else.
PII_OUTPUT = (
    "Contact the customer at alice.smith@example.com or +1 415 555 0142. "
    "Card 4111 1111 1111 1111, IBAN GB82WEST12345698765432, "
    "from IP 203.0.113.42, wallet 1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2. "
    "Their ssn is 456-78-9012."
)

# Every configured entity type, with a value the pinned Presidio release
# recognises. All of these must be masked.
MASKED_PII = (
    "alice.smith@example.com",
    "4111 1111 1111 1111",
    "GB82WEST12345698765432",
    "203.0.113.42",
    "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2",
    "456-78-9012",
)

# A bare SSN scores 0.5 in Presidio 2.2.364. It is masked, because the pinned
# Guardrails masking action applies its own hardcoded 0.4 floor and never passes
# the configured score_threshold through. Asserted below so that the gap between
# the configured 0.6 and the effective 0.4 stays visible.
BARE_SSN = "456-78-9012"
CONFIGURED_THRESHOLD = 0.6
EFFECTIVE_THRESHOLD = 0.4

SECRET_CASES = [
    "api_key=supersecretvalue12345",
    "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
    "AKIAABCDEFGHIJKLMNOP",
    "ghp_abcdefghijklmnopqrstuvwxyz123456",
    "sk-abcdefghijklmnopqrstuvwxyz123456",
    "-----BEGIN PRIVATE KEY-----",
]

# A single concrete credential leak, for tests that need one value and a
# needle to search for rather than the whole SECRET_CASES list.
LEAKED_SECRET = "The service credential is api_key=supersecretvalue12345 for the internal API."
SECRET_NEEDLE = "supersecretvalue12345"


def _deployed_rails_config():
    os.environ.setdefault("MCP_API_KEY", "verify-only-not-used")
    from nat.runtime.loader import load_config

    return load_config(str(CONFIG_PATH)).middleware["workflow_guardrails"].guardrails


def check_configuration() -> list[re.Pattern[str]]:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    middleware_cfg = config["middleware"]["workflow_guardrails"]
    guardrails = middleware_cfg["guardrails"]
    output = guardrails["rails"]["output"]
    rails_config = guardrails["rails"]["config"]

    assert middleware_cfg["stream_output_rails"] is True
    assert "regex check output" in output["flows"], output["flows"]
    assert output["streaming"]["enabled"] is True
    assert output["streaming"]["stream_first"] is False, (
        "stream_first must stay false, or a credential in the final chunk is "
        "released before the rail sees it"
    )

    # PII protection is retained for this sample. It is configurable, but if the
    # masking flow is present its entity list must be too, and vice versa.
    masking_flow = "mask sensitive data on output" in output["flows"]
    masking_configured = "sensitive_data_detection" in rails_config
    assert masking_flow == masking_configured, (
        "the masking flow and its sensitive_data_detection entity list must be "
        f"enabled together (flow={masking_flow}, config={masking_configured})"
    )
    if masking_flow:
        entities = rails_config["sensitive_data_detection"]["output"]["entities"]
        assert entities, "masking is enabled with an empty entity list"
        # PERSON and ORGANIZATION are deliberately absent: names are part of the
        # ticket-triage workflow, and masking them would destroy the answer.
        assert "PERSON" not in entities and "ORGANIZATION" not in entities, entities
        print(f"PASS: PII masking retained for {len(entities)} entity types")
    else:
        print("NOTE: PII masking is disabled in this configuration")

    patterns = rails_config["regex_detection"]["output"]["patterns"]
    compiled = [re.compile(pattern, re.IGNORECASE) for pattern in patterns]

    triggered = [p.pattern for p in compiled if p.search(TICKET_OUTPUT)]
    assert not triggered, f"ordinary ticket evidence triggers the secret rail: {triggered}"
    print("PASS: structured ticket evidence does not trigger output secret patterns")

    for secret in SECRET_CASES:
        assert any(pattern.search(secret) for pattern in compiled), secret
    print("PASS: credential/private-key leakage patterns remain blocked")

    return compiled


def check_middleware_wiring() -> None:
    incremental_source = inspect.getsource(
        TextGuardrailsMiddleware._stream_with_output_rails_incremental
    )
    assert "stream_async" in incremental_source, "NeMo streaming output rails must remain enabled"
    assert "buffered_items" not in incremental_source, (
        "the incremental output middleware must not buffer the whole assistant response"
    )
    # The rail must run on a leased instance, never the shared one: two
    # concurrent streams sharing an LLMRails let a credential in one be checked
    # against the other's text and released. Proven in verify_guardrails_rails.py.
    assert "self.rails_pool.acquire()" in incremental_source, (
        "the streaming output rail must lease an isolated rails instance"
    )
    assert "self._llm_rails.stream_async" not in incremental_source, (
        "the streaming output rail is back on the process-wide shared instance"
    )
    assert "_is_rail_block_envelope" in incremental_source, (
        "block detection must match NeMo's exact {'error': {...}} envelope, not "
        "any chunk that happens to contain an 'error' key"
    )
    print("PASS: NeMo output rails are incremental/streamed when PII masking is off")
    print("PASS: the streaming rail runs on a per-request rails instance")
    print("PASS: rail-block detection matches the exact error envelope")

    dispatch_source = inspect.getsource(TextGuardrailsMiddleware._stream_with_output_rails)
    assert "_pii_masking_enabled" in dispatch_source, (
        "the output-rail entry point must choose streaming vs. buffered mode based "
        "on whether PII masking is configured"
    )
    assert "_stream_with_buffered_masking" in dispatch_source, (
        "PII masking must be dispatched to the buffered path, not left on the "
        "streaming path where masking cannot rewrite text"
    )
    print("PASS: the output-rail entry point dispatches streaming vs. buffered mode "
          "on PII-masking configuration")

    buffered_source = inspect.getsource(TextGuardrailsMiddleware._stream_with_buffered_masking)
    assert "self.rails_pool.acquire()" in buffered_source, (
        "buffered PII masking must also run on a per-request rails instance"
    )
    assert "generate_async" in buffered_source, (
        "buffered masking must use the blocking-capable, non-streaming rail evaluation "
        "that can actually rewrite text, not stream_async"
    )
    assert "oversized" in buffered_source, (
        "an over-buffer-limit response must have a defined, safe outcome"
    )
    print("PASS: buffered PII masking runs the blocking-capable rail on an isolated instance")


async def check_masking_applies() -> None:
    """Prove the underlying masking action actually masks.

    NeMo's own *streaming* rail runner uses an action's return value only to
    decide blocked/not-blocked, never to rewrite text — that is a property of
    ``stream_async`` itself, not something a compatibility shim can change.
    ``check_streaming_pii_protection`` below proves this is a non-issue for a
    request going through this middleware: when masking is configured, the
    middleware never calls ``stream_async`` at all for that request (see
    ``_stream_with_buffered_masking``). This function checks the one thing
    that *is* still true regardless of streaming or buffering — the action
    itself masks correctly — by invoking it directly, which is simpler and
    does not require constructing a whole middleware instance to prove.
    """

    if not masking_is_available():
        print("SKIP: Presidio masking not installed; masking behaviour not exercised")
        return

    # Loading Presidio's analyzer pulls spaCy's en_core_web_lg into memory —
    # roughly 600 MB on top of whatever the agent is already using. On a Docker
    # VM that is already near capacity the kernel kills the process here, which
    # surfaces as a bare exit 137 with no message. Say so before it happens, so
    # an OOM is not mistaken for a failing assertion.
    print("NOTE: loading the Presidio analyzer (~600MB); exit 137 here means the "
          "container ran out of memory, not that a check failed")

    guardrails = _deployed_rails_config()
    if not guardrails.rails.output.flows or (
        "mask sensitive data on output" not in guardrails.rails.output.flows
    ):
        print("SKIP: masking flow not enabled in this configuration")
        return

    async def mask(text: str) -> str:
        # Called with the dispatcher-style keyword arguments the streaming runner
        # injects unconditionally, which is exactly what the pinned release
        # rejects with a TypeError without the compatibility shim.
        result = await mask_sensitive_data(
            source="output",
            text=text,
            config=guardrails,
            context={},
            llm_task_manager=None,
            model_name="",
            llms={},
            llm=None,
        )
        assert isinstance(result, str), type(result)
        return result

    masked = await mask(PII_OUTPUT)
    for secret in MASKED_PII:
        assert secret not in masked, f"{secret!r} survived PII masking: {masked}"
    print(f"PASS: all {len(MASKED_PII)} configured PII entity types are masked")

    # The replacement form is not configurable in the pinned release: matches
    # become `<ENTITY_TYPE>`, and `mask_token` is read nowhere. Asserted so the
    # config comment cannot drift from reality.
    assert "<EMAIL_ADDRESS>" in masked, masked
    assert "<CREDIT_CARD>" in masked, masked
    print("PASS: masked values are replaced with <ENTITY_TYPE> placeholders")

    # The configured score_threshold is NOT honoured by the masking action: it
    # calls Guardrails' _get_analyzer() with no threshold, which defaults to 0.4.
    # A bare SSN scores 0.5 — above the effective 0.4, below the configured 0.6 —
    # so it is masked, and it is masked *because* the configured value is ignored.
    # This asserts the discrepancy directly, so it fails loudly if a Guardrails
    # upgrade starts honouring score_threshold (at which point the config comment,
    # docs/GUARDRAILS.md and this block all need updating together).
    assert EFFECTIVE_THRESHOLD < CONFIGURED_THRESHOLD
    bare = await mask(f"Value {BARE_SSN} recorded.")
    assert BARE_SSN not in bare, (
        "the pinned Guardrails masking action now honours the configured "
        f"score_threshold of {CONFIGURED_THRESHOLD}; a bare SSN scoring 0.5 is no "
        "longer masked. Update config.yml's comment, docs/GUARDRAILS.md and this "
        "assertion together."
    )
    print(
        f"PASS (documented discrepancy): masking uses a hardcoded "
        f"{EFFECTIVE_THRESHOLD} floor, not the configured {CONFIGURED_THRESHOLD}"
    )

    # And masking must not destroy the evidence the answer is made of.
    unmasked = await mask(TICKET_OUTPUT)
    for fragment in ("TKT-1004", "EVT-1301", "172.80", "86.40", "2026-09-16", "true"):
        assert fragment in unmasked, (
            f"masking removed structured evidence {fragment!r}: {unmasked}"
        )
    print("PASS: PII masking preserves ticket identifiers, amounts and booleans")


def _build_middleware(verdict: str = "No", *, output_flows: list[str] | None = None):
    """A real ``TextGuardrailsMiddleware`` wired to the deployed rails config.

    Same technique as ``verify_guardrails_rails.py``/``verify_trace_pipeline.py``:
    the actual regex and masking output flows run unmodified; only the input
    self-check LLM is replaced with a deterministic fake, so this exercises the
    real dispatch and rail behavior, not a hand-rolled stand-in. Constructing
    it this way (rather than through NAT's ``Builder``) skips workflow
    discovery, which needs a running workflow this script does not have.
    """

    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from nemoguardrails import LLMRails
    from nat.runtime.loader import load_config

    os.environ.setdefault("MCP_API_KEY", "verify-only-not-used")
    middleware_config = load_config(str(CONFIG_PATH)).middleware["workflow_guardrails"].model_copy(deep=True)
    if output_flows is not None:
        middleware_config.guardrails.rails.output.flows = output_flows
    middleware_config.guardrails.models = []
    if getattr(middleware_config.guardrails, "tracing", None) is not None:
        try:
            middleware_config.guardrails.tracing.enabled = False
        except Exception:  # pragma: no cover - defensive, matches verify_guardrails_rails.py
            pass

    rails = LLMRails(middleware_config.guardrails, llm=FakeListChatModel(responses=[verdict] * 64))
    register_rail_compatibility(rails)

    middleware = object.__new__(TextGuardrailsMiddleware)
    middleware._llm_rails = rails
    middleware._guardrails_config = middleware_config
    middleware._config = middleware_config
    middleware._rail_llms = set()
    middleware._rail_llms_bound = True
    middleware._is_final = False
    middleware.rail_compatibility_applied = True
    middleware.rail_flow_guard = RailFlowParameterGuard(rails)
    middleware.rails_pool = RailsPool(lambda: rails)
    return middleware


def _invocation_context(args: tuple):
    from nat.middleware.middleware import FunctionMiddlewareContext
    from nat.middleware.middleware import InvocationContext

    function_context = FunctionMiddlewareContext(
        name="workflow",
        config=None,
        description=None,
        input_schema=None,
        single_output_schema=type(None),
        stream_output_schema=type(None),
    )
    return InvocationContext(
        function_context=function_context,
        original_args=args,
        original_kwargs={},
        modified_args=args,
        modified_kwargs={},
        output=None,
    )


async def _drain(middleware, ctx, call_next) -> tuple[str, int]:
    """Run the real dispatch end to end; return (released_text, yield_count)."""

    parts: list[str] = []
    async for chunk in middleware._stream_with_output_rails(ctx, call_next):
        parts.append(chunk if isinstance(chunk, str) else str(chunk))
    return "".join(parts), len(parts)


def _tokenize(text: str, size: int = 4) -> list[str]:
    return [text[index:index + size] for index in range(0, len(text), size)]


async def _chunks(parts: list[str]):
    for part in parts:
        yield part


async def check_streaming_pii_protection() -> None:
    """Behavioral proof that PII protection actually protects streamed answers.

    Drives the real dispatch (``_stream_with_output_rails`` ->
    ``_stream_with_buffered_masking``) end to end — the same entry point NAT's
    middleware pipeline calls — rather than the masking action in isolation.
    """

    if not masking_is_available():
        print("SKIP: Presidio masking not installed; streaming PII protection not exercised")
        return
    print("NOTE: loading the Presidio analyzer (~600MB); exit 137 here means the "
          "container ran out of memory, not that a check failed")

    # 1. Configured PII is masked in both a single chunk and adversarially
    #    split across many small chunks, none of which land on an entity
    #    boundary.
    for description, parts in (
        ("single chunk", [PII_OUTPUT]),
        ("adversarially split", _tokenize(PII_OUTPUT)),
    ):
        middleware = _build_middleware()
        ctx = _invocation_context(("Who's the contact for TKT-1004?",))
        released, yield_count = await _drain(middleware, ctx, lambda *_a, **_k: _chunks(parts))
        for secret in MASKED_PII:
            assert secret not in released, f"{description}: {secret!r} leaked: {released}"
        assert yield_count <= 1, (
            f"{description}: expected one buffered release, got {yield_count} chunks"
        )
        print(f"PASS: streamed PII protection masks configured entities ({description})")

    # 2. Nothing is released before masking completes: the entire upstream
    #    must already be consumed by the time the (single) release happens.
    consumed_before_yield: list[str] = []

    async def tracking_call_next(*_args, **_kwargs):
        for part in _tokenize(PII_OUTPUT):
            consumed_before_yield.append(part)
            yield part

    middleware = _build_middleware()
    ctx = _invocation_context(("q",))
    agen = middleware._stream_with_output_rails(ctx, tracking_call_next)
    first = await agen.__anext__()
    assert "".join(consumed_before_yield) == PII_OUTPUT, "upstream was not fully drained before releasing"
    first_text = first if isinstance(first, str) else str(first)
    for secret in MASKED_PII:
        assert secret not in first_text, f"a raw prefix leaked before masking completed: {first_text}"
    await agen.aclose()
    print("PASS: no raw sensitive prefix is released before masking completes")

    # 3. Secret blocking still works when PII masking is also configured.
    middleware = _build_middleware()
    ctx = _invocation_context(("q",))
    released, _ = await _drain(middleware, ctx, lambda *_a, **_k: _chunks(_tokenize(LEAKED_SECRET)))
    assert SECRET_NEEDLE not in released, released
    print("PASS: secret blocking still works with PII masking enabled")

    # 4. Benign structured output survives buffered masking intact.
    middleware = _build_middleware()
    ctx = _invocation_context(("q",))
    released, _ = await _drain(middleware, ctx, lambda *_a, **_k: _chunks(_tokenize(TICKET_OUTPUT)))
    for fragment in ("TKT-1004", "EVT-1301", "172.80", "86.40", "2026-09-16", "true"):
        assert fragment in released, f"{fragment!r} lost to buffered masking: {released}"
    print("PASS: benign structured ticket evidence survives buffered PII masking intact")

    # 5. Concurrent benign and sensitive streamed responses stay isolated at
    #    the *middleware-instance* boundary: two independent middleware
    #    instances, each with its own rails_pool, standing in for two
    #    concurrent requests hitting two different workers/processes. This
    #    does not exercise RailsPool's own instance-isolation guarantee —
    #    each middleware here never shares a pool with the other, so there is
    #    nothing for the pool to isolate. `check_shared_middleware_pool_concurrency`
    #    below is what proves that: one middleware, one pool, two concurrent
    #    leases from the same production factory.
    async def drip(parts: list[str], delay: float):
        for part in parts:
            await asyncio.sleep(delay)
            yield part

    benign_middleware = _build_middleware()
    sensitive_middleware = _build_middleware()
    (released_benign, _), (released_sensitive, _) = await asyncio.gather(
        _drain(
            benign_middleware,
            _invocation_context(("q",)),
            lambda *_a, **_k: drip(_tokenize(TICKET_OUTPUT), 0.001),
        ),
        _drain(
            sensitive_middleware,
            _invocation_context(("q",)),
            lambda *_a, **_k: drip(_tokenize(PII_OUTPUT), 0.0015),
        ),
    )
    for fragment in ("TKT-1004", "172.80"):
        assert fragment in released_benign, released_benign
    for secret in MASKED_PII:
        assert secret not in released_benign, f"the sensitive stream leaked into the benign one: {released_benign}"
        assert secret not in released_sensitive, released_sensitive
    print("PASS: concurrent benign and sensitive streamed responses stay isolated")

    # 6. A response too large to safely buffer is refused, not leaked.
    previous_limit = os.environ.get("GUARDRAILS_PII_MAX_BUFFER_CHARS")
    os.environ["GUARDRAILS_PII_MAX_BUFFER_CHARS"] = "1024"
    try:
        middleware = _build_middleware()
        ctx = _invocation_context(("q",))
        oversized_text = PII_OUTPUT * 200  # comfortably over the 1024-char cap
        released, yield_count = await _drain(
            middleware, ctx, lambda *_a, **_k: _chunks(_tokenize(oversized_text))
        )
        for secret in MASKED_PII:
            assert secret not in released, f"an oversized response leaked PII: {released}"
        assert released, "an oversized response must still yield a defined, safe refusal"
        assert yield_count <= 1
        print("PASS: a response exceeding the PII buffer limit is refused safely, not leaked")
    finally:
        if previous_limit is None:
            os.environ.pop("GUARDRAILS_PII_MAX_BUFFER_CHARS", None)
        else:
            os.environ["GUARDRAILS_PII_MAX_BUFFER_CHARS"] = previous_limit

    # 7. Disabling PII protection has documented, unmasked behavior — and
    #    keeps the regex-only path on the fast, fully-streamed dispatch.
    middleware = _build_middleware(output_flows=["regex check output"])
    assert middleware._pii_masking_enabled() is False
    ctx = _invocation_context(("q",))
    released, _ = await _drain(middleware, ctx, lambda *_a, **_k: _chunks(_tokenize(PII_OUTPUT)))
    assert released.strip() == PII_OUTPUT.strip(), (
        "PII masking flow was disabled but the text changed anyway"
    )
    print("PASS: disabling the PII masking flow leaves PII unmasked (documented) and stays "
          "on the streaming dispatch")


class _Boom(Exception):
    """A synthetic upstream failure, distinct from any real exception type."""


async def _assert_lease_returned_and_reused(
    pool: RailsPool,
    *,
    previously_held_ids: set[int],
    timeout: float = 5.0,
) -> None:
    """Prove a just-released lease was actually returned to ``pool``, two ways.

    Checking ``pool.built`` alone proves neither half of this: a leaked
    semaphore permit and a separately-broken idle-return could each still
    leave ``built`` unchanged by coincidence (nothing forced a new instance
    to be built), while the pool itself is quietly running one slot short.

    1. A sequential re-acquire must hand back one of the instance ids in
       ``previously_held_ids`` — proof the release actually reached
       ``_idle`` and is being reused, not that a fresh instance happened to
       be built. ``_idle.pop()`` is LIFO, and nothing else is contending for
       the pool at this point in the test, so the very next acquire is
       exactly the entry just released.
    2. Every slot in the pool must be concurrently re-acquirable — and held
       open *simultaneously* — within ``timeout``. Sequential acquire/
       release cannot show this: with a leaked permit, ``size - 1``
       sequential round-trips still each succeed in turn, one at a time.
       Only holding all of them at once makes a missing slot observable, and
       ``asyncio.Event`` (not a sleep-based guess) confirms each one is
       genuinely held before this function decides they all are.
    """

    async with pool.acquire() as rails:
        assert id(rails) in previously_held_ids, (
            f"a re-acquire built or fetched an instance ({id(rails)}) that was never "
            f"one of the ones just released ({previously_held_ids}); the released "
            f"lease was not returned to the idle pool for reuse"
        )

    release_event = asyncio.Event()
    acquired_events = [asyncio.Event() for _ in range(pool.size)]
    slot_ids: list[int | None] = [None] * pool.size

    async def hold(index: int) -> None:
        async with pool.acquire() as rails:
            slot_ids[index] = id(rails)
            acquired_events[index].set()
            await release_event.wait()

    tasks = [asyncio.ensure_future(hold(index)) for index in range(pool.size)]
    try:
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in acquired_events)),
            timeout=timeout,
        )
    finally:
        release_event.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=timeout)

    assert len(set(slot_ids)) == pool.size, (
        f"could not hold all {pool.size} pool slots concurrently and simultaneously — "
        f"a slot or semaphore permit was not released: {slot_ids}"
    )


async def check_shared_middleware_pool_concurrency() -> None:
    """RailsPool's own instance-isolation guarantee, under one shared middleware.

    Every other concurrency check in this file — including step 5 above —
    uses two *independent* middleware instances, each with its own pool. That
    proves request-level isolation, but nothing there ever shares a
    ``RailsPool``, so it cannot prove the pool itself hands two concurrent
    callers genuinely separate ``LLMRails`` instances rather than the same one
    twice. This test builds ONE middleware and replaces only its pool's
    factory with the production one (``TextGuardrailsMiddleware._build_rails``,
    the same method the real middleware uses to grow its own pool) — not a
    ``lambda: rails`` returning one shared instance, and not a second
    middleware. Two concurrent requests then lease from that single pool.
    """

    if not masking_is_available():
        print("SKIP: Presidio masking not installed; shared-pool concurrency not exercised")
        return

    middleware = _build_middleware()
    middleware.rails_pool = RailsPool(middleware._build_rails, size=4)

    # Part 1: two concurrent requests through the REAL middleware dispatch
    # (_stream_with_output_rails -> _stream_with_buffered_masking), on one
    # shared instance's production pool. Proves no cross-contamination in
    # content, masking or blocking. This alone does not reliably force the
    # two pool leases to *overlap*: _stream_with_buffered_masking only holds
    # a lease for the brief generate_async call, after the whole answer is
    # buffered, and Presidio's analysis runs synchronously inside it with no
    # scheduler-visible await point -- so one request's whole
    # acquire-evaluate-release can complete in a single scheduling turn
    # before the other even starts. Genuine lease overlap is proven
    # separately, in Part 2.
    async def drip(parts: list[str], delay: float):
        for part in parts:
            await asyncio.sleep(delay)
            yield part

    released_benign, released_sensitive = await asyncio.gather(
        _drain(
            middleware,
            _invocation_context(("q",)),
            lambda *_a, **_k: drip(_tokenize(TICKET_OUTPUT), 0.001),
        ),
        _drain(
            middleware,
            _invocation_context(("q",)),
            lambda *_a, **_k: drip(_tokenize(PII_OUTPUT), 0.0015),
        ),
    )
    released_benign_text, _ = released_benign
    released_sensitive_text, _ = released_sensitive

    for fragment in ("TKT-1004", "172.80"):
        assert fragment in released_benign_text, released_benign_text
    for secret in MASKED_PII:
        assert secret not in released_benign_text, (
            f"the sensitive request's content crossed into the benign one via a shared "
            f"pool instance: {released_benign_text}"
        )
        assert secret not in released_sensitive_text, released_sensitive_text
    print("PASS: two concurrent requests on one shared middleware's production RailsPool "
          "do not cross-contaminate answers, and masking/blocking stays correct")

    # Part 2: force the pool-lease windows to overlap, on the same shared
    # pool, so the factory is proven to build genuinely independent
    # instances under real concurrency rather than only ever serializing
    # through instance reuse. Paced deliberately (mirroring how
    # verify_guardrails_rails.py paces its own concurrency proofs) rather
    # than left to scheduler luck.
    async def evaluate_via_pool(text: str, hold_open: float):
        async with middleware.rails_pool.acquire() as rails:
            leased_instance_id = id(rails)
            await asyncio.sleep(hold_open)
            # A preceding user turn is required for NeMo to treat the
            # assistant message as a response to check, not just echo it
            # back unevaluated -- the same two-message shape post_invoke
            # itself builds whenever there is a user turn to attach.
            response = await rails.generate_async(
                messages=[{"role": "user", "content": "q"}, {"role": "assistant", "content": text}],
                options=GenerationOptions(
                    rails=["output"],
                    log=GenerationLogOptions(activated_rails=True),
                    output_vars=["bot_message", "user_message"],
                ),
            )
        return leased_instance_id, response

    (benign_instance_id, benign_response), (secret_instance_id, secret_response) = await asyncio.gather(
        evaluate_via_pool(TICKET_OUTPUT, 0.05),
        evaluate_via_pool(LEAKED_SECRET, 0.05),
    )
    # The direct, unambiguous proof: while both leases were open at once
    # (each held for the full 0.05s `hold_open` window, guaranteeing
    # overlap), they were genuinely two different LLMRails objects -- not
    # the same shared instance handed out twice. Whether the pool built
    # these two just now or reused two it already had idle from Part 1 does
    # not matter; either way is correct pool behaviour, and `built` alone
    # cannot distinguish "reused two existing instances concurrently" (safe)
    # from "handed out one instance twice" (the bug RailsPool exists to
    # prevent), so identity is what is actually asserted.
    assert benign_instance_id != secret_instance_id, (
        "two overlapping concurrent leases were handed the same rails instance"
    )
    assert middleware.rails_pool.built >= 2, (
        f"at least two distinct pool instances must exist, built={middleware.rails_pool.built}"
    )
    assert not middleware._rail_blocked(benign_response), "benign evidence was blocked under concurrent load"
    assert middleware._rail_blocked(secret_response), "a concurrent leaked secret was not blocked"
    print("PASS: overlapping concurrent pool leases are genuinely independent rail "
          "instances, and each is still evaluated correctly")

    # The remaining three cases prove RailsPool.acquire()'s own cleanup
    # contract: a lease is returned on success, on the caller raising while
    # holding it, and on the caller being cancelled while holding it. Each
    # one acquires the lease *directly*, not through
    # _stream_with_buffered_masking: that method only acquires a lease for
    # the brief generate_async call, strictly after the entire answer has
    # already been consumed from call_next. A fault raised (or a
    # cancellation delivered) from inside call_next -- as the previous
    # version of this test did -- unwinds before any lease is ever taken, so
    # it cannot exercise this contract at all; "pool.built did not change"
    # was trivially true because nothing was ever leased. Acquiring directly
    # is what the pool's own docstring describes protecting against, and
    # matches the equivalent, already-reviewed pattern in
    # verify_guardrails_rails.py's "a leased instance is returned to the
    # pool on caller error" check.

    # Case 1: a normal, successful release.
    async with middleware.rails_pool.acquire() as rails:
        released_id = id(rails)
    await _assert_lease_returned_and_reused(middleware.rails_pool, previously_held_ids={released_id})
    print("PASS: a lease released after normal success is returned to the pool and reused")

    # Case 2: the caller raises while the lease is actively held. `_Boom` is
    # a synthetic, test-only exception standing in for any failure that
    # could occur while a lease is checked out (e.g. a rail evaluation
    # error) -- the pool's cleanup contract does not depend on which
    # exception type unwinds through it.
    released_id = None

    async def fail_while_leased() -> None:
        nonlocal released_id
        async with middleware.rails_pool.acquire() as rails:
            released_id = id(rails)
            raise _Boom("synthetic failure while a pool lease is actively held")

    try:
        await fail_while_leased()
    except _Boom:
        pass
    else:
        raise AssertionError("the synthetic failure did not propagate out of an active lease")
    await _assert_lease_returned_and_reused(middleware.rails_pool, previously_held_ids={released_id})
    print("PASS: a caller failure while a pool lease is actively held still returns and reuses that lease")

    # Case 3: the caller is cancelled while the lease is actively held.
    # asyncio.Event, not a sleep, is what proves the lease was genuinely
    # acquired before cancellation is requested: `lease_acquired.set()` runs
    # only after `async with middleware.rails_pool.acquire()` has already
    # entered, so `task.cancel()` below is guaranteed to land while the
    # lease is held rather than merely likely to.
    lease_acquired = asyncio.Event()
    released_id = None

    async def hold_lease_until_cancelled() -> None:
        nonlocal released_id
        async with middleware.rails_pool.acquire() as rails:
            released_id = id(rails)
            lease_acquired.set()
            await asyncio.sleep(3600)  # cancelled well before this could elapse

    task = asyncio.ensure_future(hold_lease_until_cancelled())
    await asyncio.wait_for(lease_acquired.wait(), timeout=5.0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("the task was cancelled but did not raise CancelledError")
    await _assert_lease_returned_and_reused(middleware.rails_pool, previously_held_ids={released_id})
    print("PASS: cancellation while a pool lease is actively held still returns and reuses that lease")


def main() -> None:
    check_configuration()
    check_middleware_wiring()
    asyncio.run(check_masking_applies())
    asyncio.run(check_streaming_pii_protection())
    asyncio.run(check_shared_middleware_pool_concurrency())


if __name__ == "__main__":
    main()
