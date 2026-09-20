# SPDX-License-Identifier: Apache-2.0
"""Application-side compatibility layer for the NeMo Guardrails output rails.

Why this exists
---------------
``nemoguardrails`` 0.21 — the release ``nvidia-nat-security[guardrails]==1.8.0``
pins — ships the actions behind our configured output rails with three defects
that make the rails unreliable on the *streaming* path:

1. ``detect_regex_pattern`` (the ``regex check output`` rail) is declared without
   an ``output_mapping``. NeMo decides whether a streamed chunk is blocked with
   ``nemoguardrails.actions.output_mapping.is_output_blocked``. Without an
   explicit mapping it falls back to ``default_output_mapping``, which returns
   ``False`` for any non-bool, non-numeric result. ``detect_regex_pattern``
   returns a ``RegexDetectionResult`` dict, so a match never blocks the stream.

2. ``detect_regex_pattern`` and ``mask_sensitive_data`` are both declared as
   ``(source, text, config)``. The streaming runner
   (``LLMRails._run_output_rails_in_streaming._prepare_params``) always passes
   ``context``, ``llm_task_manager``, ``config``, ``model_name``, ``llms`` and
   ``llm``, so calling either action raises ``TypeError`` before any check runs.
   The non-streaming Colang runtime filters parameters by signature, which is
   why the defect only appears when ``rails.output.streaming.enabled`` is set.
   (``detect_sensitive_data`` in the same module already accepts ``**kwargs``
   and already declares a mapping, so it needs nothing.)

3. ``_prepare_params`` resolves the ``$bot_message`` placeholder **in place**
   into the ``action_params`` dict it receives, and
   ``get_action_details_from_flow_id`` hands out a direct reference into the
   shared, process-wide flow configuration. The first streamed response
   therefore permanently rewrites ``text: "$bot_message"`` to that response's
   literal text, and every later request re-checks the first request's output
   instead of its own. For a secret-leakage rail that is a silent security
   failure after request one.

All three are fixed upstream in ``nemoguardrails`` 0.23.0
(``_regex_blocked_mapping`` + ``**kwargs`` in the actions; a defensive ``dict()``
copy in ``get_action_details_from_flow_id`` and ``_prepare_params``). That
release cannot be installed here: ``nvidia-nat-security[guardrails]==1.8.0``
requires ``nemoguardrails>=0.11,<0.22``, so 0.22.0 and 0.23.0 are both outside
the supported dependency range.

Rather than rewriting the installed package at image-build time, this module:

* re-declares each affected action correctly and registers it through the
  documented NeMo Guardrails extension point ``LLMRails.register_action()`` —
  each wrapper delegates to the installed upstream implementation, so detection
  and masking logic is never forked, only its declaration is corrected;
* snapshots the pristine rail flow parameters of *our own* ``LLMRails``
  instance and restores them before each rail invocation, which neutralises the
  in-place mutation without touching third-party code;
* hands each concurrent rail evaluation its own instance, because restoring
  between invocations is only sufficient for one request at a time.

What masking can and cannot do on the streaming path
----------------------------------------------------
Worth stating plainly, because the configuration reads as though it should do
more: on the *streaming* output path NeMo uses an action's return value **only**
to decide blocked/not-blocked — the chunk it yields is the unmodified upstream
chunk. So ``mask sensitive data on output`` cannot rewrite streamed text; the
``**kwargs`` fix stops it raising ``TypeError`` and makes it a working no-op
there. Deterministic PII masking takes effect on the non-streaming path
(``rails.output.streaming.enabled: false``, or the blocking generate call used
by the input rail). ``regex check output`` does block streamed chunks once
defect 1 is fixed, which is why credential patterns are enforced by the regex
rail rather than by masking. ``verify_output_guardrails.py`` asserts both halves
of this.

Removal condition
-----------------
Every helper here is self-disabling: each becomes a no-op as soon as the
installed action declares what it needs. Delete this module once
``nvidia-nat-security`` relaxes its Guardrails pin to a release >= 0.23.0 and
``requirements.txt`` is upgraded accordingly.

Private-API reliance: none. ``LLMRails.register_action``,
``register_action_param``, ``rails.config.flows`` and ``action_meta`` are all
public. The flow-parameter guard does reach into the *structure* of parsed
Colang flow elements (``_type``/``action_params`` keys), which is internal data
rather than an API; it is read defensively and degrades to doing nothing.
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

#: Presidio masking action, used by the ``mask sensitive data on output`` rail.
MASK_ACTION_NAME = "mask_sensitive_data"


def _accepts_var_keyword(function: Any) -> bool:
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )


def _declared_output_mapping(function: Any) -> Any:
    return (getattr(function, "action_meta", None) or {}).get("output_mapping")


# ---------------------------------------------------------------------------
# Defect 1 + 2: the regex output rail
# ---------------------------------------------------------------------------


def regex_blocked_mapping(result: Any) -> bool:
    """Return True when a forbidden regex pattern matched.

    Mirrors ``nemoguardrails.library.regex.actions._regex_blocked_mapping`` as
    released in 0.23.0. NeMo calls it with the action's return value to decide
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
    """Return True when the installed regex action needs no compatibility shim."""

    if _declared_output_mapping(_upstream_detect_regex_pattern) is None:
        return False
    return _accepts_var_keyword(_upstream_detect_regex_pattern)


# ---------------------------------------------------------------------------
# Defect 2: the Presidio masking output rail
#
# Imported lazily and tolerantly: the `sdd` extra that provides Presidio is
# optional. A deployment that configures no masking rail must not fail to start
# just because the analyzer is not installed.
# ---------------------------------------------------------------------------

try:
    from nemoguardrails.library.sensitive_data_detection.actions import (
        mask_sensitive_data as _upstream_mask_sensitive_data,
    )
except Exception as _mask_import_error:  # pragma: no cover - optional extra
    _upstream_mask_sensitive_data = None
    logger.info(
        "Presidio masking action unavailable (%s); the masking compatibility shim is inactive.",
        _mask_import_error,
    )


def masking_is_available() -> bool:
    """Whether the optional Presidio masking action is installed at all."""

    return _upstream_mask_sensitive_data is not None


def upstream_mask_action_is_fixed() -> bool:
    """Return True when the installed masking action needs no compatibility shim."""

    if _upstream_mask_sensitive_data is None:
        return True
    # Deliberately does NOT require an output_mapping. A masking action must not
    # block: with no mapping, NeMo's default returns False for its string result,
    # which is the correct behaviour. Only the signature is wrong in 0.21.
    return _accepts_var_keyword(_upstream_mask_sensitive_data)


@action(name=MASK_ACTION_NAME, is_system_action=True)
async def mask_sensitive_data(
    source: str,
    text: str,
    config: Any,
    **kwargs: Any,
) -> Any:
    """Signature-compatible wrapper around the installed Presidio masking action.

    Only ``**kwargs`` is added, for the same reason as the regex wrapper. No
    ``output_mapping`` is declared on purpose — see
    ``upstream_mask_action_is_fixed``.
    """

    assert _upstream_mask_sensitive_data is not None  # guarded by registration
    return await _upstream_mask_sensitive_data(source=source, text=text, config=config)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_rail_compatibility(rails: Any) -> tuple[str, ...]:
    """Register corrected action declarations on an ``LLMRails`` instance.

    Returns the names of the actions that were re-registered. An empty tuple
    means the installed ``nemoguardrails`` release already declares everything
    correctly and this module can be deleted.
    """

    registered: list[str] = []

    if upstream_regex_action_is_fixed():
        logger.info(
            "Installed NeMo Guardrails already declares a blocking regex output "
            "rail; skipping the compatibility registration.",
        )
    else:
        rails.register_action(detect_regex_pattern, REGEX_ACTION_NAME)
        registered.append(REGEX_ACTION_NAME)

    if not masking_is_available():
        logger.debug("No Presidio masking action installed; nothing to correct.")
    elif upstream_mask_action_is_fixed():
        logger.info(
            "Installed NeMo Guardrails masking action already accepts dispatcher "
            "keyword arguments; skipping the compatibility registration.",
        )
    else:
        rails.register_action(mask_sensitive_data, MASK_ACTION_NAME)
        registered.append(MASK_ACTION_NAME)

    if registered:
        logger.info(
            "Registered corrected Guardrails action(s) %s through the supported "
            "LLMRails.register_action extension point.",
            ", ".join(registered),
        )
    return tuple(registered)


def compatibility_required() -> bool:
    """Whether the installed Guardrails release needs any shim from this module."""

    return not upstream_regex_action_is_fixed() or (
        masking_is_available() and not upstream_mask_action_is_fixed()
    )


# ---------------------------------------------------------------------------
# Defect 3: shared flow parameters mutated in place
# ---------------------------------------------------------------------------


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
        self._enabled = compatibility_required()
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

    @property
    def guarded_parameter_sets(self) -> int:
        """How many flow parameter dicts are being protected. Exposed for tests."""

        return len(self._snapshot)

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
            logger.debug(
                "Restored NeMo Guardrails flow parameters mutated by the streaming rail runner."
            )
        return restored


# ---------------------------------------------------------------------------
# Concurrency: one instance per in-flight rail evaluation
# ---------------------------------------------------------------------------

#: Concurrent rail evaluations that may run without waiting for each other.
DEFAULT_RAIL_POOL_SIZE = 4


def _pool_size() -> int:
    raw = os.getenv("GUARDRAILS_RAIL_POOL_SIZE", str(DEFAULT_RAIL_POOL_SIZE))
    try:
        return max(int(raw), 1)
    except ValueError:
        logger.warning(
            "GUARDRAILS_RAIL_POOL_SIZE=%r is not an integer; using %d",
            raw,
            DEFAULT_RAIL_POOL_SIZE,
        )
        return DEFAULT_RAIL_POOL_SIZE


class RailsPool:
    """Hand each concurrent rail evaluation its own ``LLMRails``.

    ``RailFlowParameterGuard`` restores the mutated ``$bot_message`` placeholder
    between invocations, which is exactly enough for one request at a time. It is
    not enough for two. The middleware holds a single long-lived ``LLMRails``, and
    NeMo resolves the placeholder into that instance's flow configuration *during*
    streaming, so with two responses in flight one stream's chunks get evaluated
    against the other stream's text — and a secret in the stream that was not
    being checked walks straight through.

    Instances are pooled rather than built per request because construction is
    not free — too much to pay on every response, nothing to pay once. Beyond the
    pool size, evaluations queue instead of sharing, so the safety property holds
    under any load and only throughput degrades.
    """

    def __init__(self, factory: Callable[[], Any], size: int | None = None) -> None:
        self._factory = factory
        self._size = size if size is not None else _pool_size()
        # No lock guards _idle. asyncio runs one coroutine at a time, and every
        # access below is a single list operation with no await in between, so
        # the list is already safe. A lock here would additionally have to be
        # awaited from the `finally` of a cancelled task, where awaiting is
        # exactly what must not happen.
        self._idle: list[tuple[Any, RailFlowParameterGuard]] = []
        self._slots = asyncio.Semaphore(self._size)
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
            # Returned on success, on exception and on cancellation alike, so a
            # failed or abandoned request cannot permanently shrink the pool.
            if entry is not None:
                self._idle.append(entry)
            self._slots.release()
