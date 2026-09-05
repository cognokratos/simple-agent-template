# SPDX-License-Identifier: Apache-2.0
"""Application-side compatibility layer for the NeMo Guardrails regex rail.

Why this exists
---------------
``nemoguardrails`` 0.21 ships ``detect_regex_pattern`` (the action behind the
``regex check output`` rail) with two defects that make the rail a no-op on the
*streaming* output path:

1. The action is declared without an ``output_mapping``. NeMo decides whether a
   streamed chunk is blocked with
   ``nemoguardrails.actions.output_mapping.is_output_blocked``. Without an
   explicit mapping it falls back to ``default_output_mapping``, which returns
   ``False`` for any non-bool, non-numeric result. ``detect_regex_pattern``
   returns a ``RegexDetectionResult`` dict, so a match never blocks the stream.

2. The action signature is ``(source, text, config)``. The streaming runner
   (``LLMRails._run_output_rails_in_streaming._prepare_params``) always passes
   ``context``, ``llm_task_manager``, ``config``, ``model_name``, ``llms`` and
   ``llm``, so calling the action raises ``TypeError`` before any regex runs.
   The non-streaming Colang runtime filters parameters by signature, which is
   why the defect only appears when ``rails.output.streaming.enabled`` is set.

3. ``LLMRails._run_output_rails_in_streaming._prepare_params`` resolves the
   ``$bot_message`` placeholder **in place** into the ``action_params`` dict it
   receives, and ``get_action_details_from_flow_id`` hands out a direct
   reference into the shared, process-wide flow configuration. The first
   streamed response therefore permanently rewrites ``text: "$bot_message"`` to
   that response's literal text, and every later request re-checks the first
   request's output instead of its own. For a secret-leakage rail that is a
   silent security failure after request one.

All three defects are fixed verbatim upstream in ``nemoguardrails`` 0.23.0
(``_regex_blocked_mapping`` + ``**kwargs`` in the action; a defensive ``dict()``
copy in ``get_action_details_from_flow_id`` and ``_prepare_params``). That
release cannot be installed here: ``nvidia-nat-security[guardrails]==1.8.0``
requires ``nemoguardrails>=0.11,<0.22``, so 0.22.0 and 0.23.0 are both outside
the supported dependency range.

Rather than rewriting the installed package, this module:

* re-declares the action correctly and registers it through the documented
  NeMo Guardrails extension point ``LLMRails.register_action()`` — the wrapper
  delegates to the installed upstream implementation, so detection logic is
  never forked, only its declaration is corrected;
* snapshots the pristine rail flow parameters of *our own* ``LLMRails``
  instance and restores them before each rail invocation, which neutralises the
  in-place mutation without touching third-party code.

Removal condition
-----------------
Both helpers are self-disabling: they become no-ops as soon as the installed
``detect_regex_pattern`` declares an ``output_mapping`` and accepts ``**kwargs``.
Delete this module once ``nvidia-nat-security`` relaxes its Guardrails pin to a
release >= 0.23.0 and ``requirements.txt`` is upgraded accordingly.
"""

import asyncio
import contextlib
import copy
import inspect
import logging
import os
from collections.abc import AsyncIterator
from collections.abc import Callable
from typing import Any

from nemoguardrails.actions import action
from nemoguardrails.library.regex.actions import RegexDetectionResult
from nemoguardrails.library.regex.actions import detect_regex_pattern as _upstream_detect_regex_pattern

logger = logging.getLogger(__name__)

#: Action name used by the bundled Colang flows (``execute detect_regex_pattern``).
REGEX_ACTION_NAME = "detect_regex_pattern"


def regex_blocked_mapping(result: Any) -> bool:
    """Return True when a forbidden regex pattern matched.

    This mirrors ``nemoguardrails.library.regex.actions._regex_blocked_mapping``
    as released in 0.23.0. NeMo calls it with the action's return value to decide
    whether a streamed chunk must be blocked.
    """

    if isinstance(result, dict):
        return bool(result.get("is_match", False))
    return bool(getattr(result, "is_match", False))


@action(
    name=REGEX_ACTION_NAME,
    is_system_action=True,
    output_mapping=regex_blocked_mapping,
)
async def detect_regex_pattern(
    source: str,
    text: str,
    config: Any,
    **kwargs: Any,
) -> RegexDetectionResult:
    """Blocking-aware wrapper around the installed regex detection action.

    ``**kwargs`` absorbs the parameters the streaming runner injects
    unconditionally (``context``, ``llm_task_manager``, ``model_name``, ``llms``,
    ``llm``). The matching itself is delegated to the upstream implementation.
    """

    return await _upstream_detect_regex_pattern(source=source, text=text, config=config)


def upstream_regex_action_is_fixed() -> bool:
    """Return True when the installed Guardrails release needs no compatibility shim."""

    meta = getattr(_upstream_detect_regex_pattern, "action_meta", None) or {}
    if meta.get("output_mapping") is None:
        return False

    parameters = inspect.signature(_upstream_detect_regex_pattern).parameters
    return any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())


def register_regex_rail_compatibility(rails: Any) -> bool:
    """Register the corrected regex action on an ``LLMRails`` instance.

    Returns True when the shim was installed, False when the installed
    ``nemoguardrails`` release already declares the action correctly.
    """

    if upstream_regex_action_is_fixed():
        logger.info(
            "Installed NeMo Guardrails already declares a blocking regex output "
            "rail; skipping the compatibility registration.",
        )
        return False

    rails.register_action(detect_regex_pattern, REGEX_ACTION_NAME)
    logger.info(
        "Registered the blocking %r Guardrails action through the supported "
        "LLMRails.register_action extension point.",
        REGEX_ACTION_NAME,
    )
    return True


class RailFlowParameterGuard:
    """Keep a rail policy's flow parameters pristine across requests.

    NeMo Guardrails 0.21 resolves ``$bot_message``/``$user_message`` placeholders
    directly into the shared flow configuration while running streaming output
    rails. Once resolved, the placeholder is gone for the lifetime of the
    process and every later request evaluates the *first* request's text.

    This guard snapshots the placeholders of the ``LLMRails`` instance owned by
    this application and restores them before each rail invocation. It only ever
    writes to configuration data owned by that instance.
    """

    def __init__(self, rails: Any) -> None:
        self._enabled = not upstream_regex_action_is_fixed()
        self._snapshot: list[tuple[dict[str, Any], dict[str, Any]]] = []
        if not self._enabled:
            return

        for flow in getattr(getattr(rails, "config", None), "flows", None) or []:
            elements = flow.get("elements") if isinstance(flow, dict) else None
            for element in elements or []:
                if not isinstance(element, dict) or element.get("_type") != "run_action":
                    continue
                params = element.get("action_params")
                if isinstance(params, dict) and params:
                    self._snapshot.append((params, copy.deepcopy(params)))

    @property
    def enabled(self) -> bool:
        return self._enabled

    def restore(self) -> bool:
        """Reset any flow parameter mutated by a previous rail invocation.

        Returns True when at least one parameter had to be restored.
        """

        restored = False
        for params, pristine in self._snapshot:
            if params != pristine:
                params.clear()
                params.update(copy.deepcopy(pristine))
                restored = True
        if restored:
            logger.debug("Restored NeMo Guardrails flow parameters mutated by the streaming rail runner.")
        return restored


#: Concurrent rail evaluations that may run without waiting for each other.
DEFAULT_RAIL_POOL_SIZE = 4


def _pool_size() -> int:
    try:
        return max(int(os.getenv("GUARDRAILS_RAIL_POOL_SIZE", str(DEFAULT_RAIL_POOL_SIZE))), 1)
    except ValueError:
        return DEFAULT_RAIL_POOL_SIZE


class RailsPool:
    """Hand each concurrent rail evaluation its own ``LLMRails``.

    ``RailFlowParameterGuard`` restores the mutated ``$bot_message`` placeholder
    between invocations, which is exactly enough for one request at a time. It is
    not enough for two. The middleware holds a single long-lived ``LLMRails``, and
    NeMo resolves the placeholder into that instance's flow configuration *during*
    streaming, so with two responses in flight one stream's chunks get evaluated
    against the other stream's text.

    That is measured, not inferred: ``verify_guardrails_rails.py`` streams a
    credential concurrently with a benign response, and before this pool existed
    the credential was released in full while the rail reported nothing.

    Instances are pooled rather than built per request because construction costs
    roughly 70 ms — too much to pay on every response, nothing to pay once. Beyond
    the pool size, evaluations queue instead of sharing, so the safety property
    holds under any load and only throughput degrades.
    """

    def __init__(self, factory: Callable[[], Any], size: int | None = None) -> None:
        self._factory = factory
        self._size = size if size is not None else _pool_size()
        self._idle: list[tuple[Any, RailFlowParameterGuard]] = []
        self._slots = asyncio.Semaphore(self._size)
        self._lock = asyncio.Lock()
        self._built = 0

    @property
    def size(self) -> int:
        return self._size

    @property
    def built(self) -> int:
        """Instances actually constructed. Exposed for tests."""

        return self._built

    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncIterator[Any]:
        """Lease an instance no other task is using for the duration of the block."""

        await self._slots.acquire()
        entry: tuple[Any, RailFlowParameterGuard] | None = None
        try:
            async with self._lock:
                if self._idle:
                    entry = self._idle.pop()
            if entry is None:
                rails = self._factory()
                entry = (rails, RailFlowParameterGuard(rails))
                self._built += 1
                logger.info(
                    "Built guardrails instance %d of %d for concurrent rail evaluation",
                    self._built,
                    self._size,
                )
            # This instance may have been used before, so it still needs the
            # sequential guard; the pool only removes the *concurrent* sharing.
            entry[1].restore()
            yield entry[0]
        finally:
            if entry is not None:
                async with self._lock:
                    self._idle.append(entry)
            self._slots.release()
