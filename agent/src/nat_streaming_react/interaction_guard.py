# SPDX-License-Identifier: Apache-2.0
"""Authorization for human-in-the-loop interaction responses.

The gap this closes
-------------------
NAT's HTTP interaction endpoint is::

    POST /executions/{execution_id}/interactions/{interaction_id}/response

Its handler (``nat/front_ends/fastapi/routes/execution.py``) calls
``ExecutionStore.resolve_interaction(execution_id, interaction_id, response)``
and nothing else. It does not consider who is asking, and ``ExecutionRecord``
carries no owner. So in stock NAT, *knowing two UUIDs is sufficient authority to
answer somebody else's approval prompt* — and to answer it with any choice the
schema permits, including one the prompt never offered.

That is not adequate for an approval boundary. Knowledge of an identifier is not
authorization, and a pending prompt is not an open-ended form.

What this module enforces
-------------------------
Two properties, both checked before NAT's resolution runs:

1. **The responder owns the execution.** The authenticated user recorded when
   the prompt was created must be the authenticated user submitting the
   response. A second, equally authenticated user cannot resolve a prompt that
   was not addressed to them.

2. **The submitted choice was actually offered.** For a choice-bearing prompt,
   the submitted option must correspond *exactly* — the same id **and** the
   same value, from the *same* offered option — to one this server put in
   *that* prompt. Checking id and value independently (whether either one
   appears anywhere across the whole offered set) is not the same check: it
   lets a response mix an offered id with an unoffered value, or two
   individually-offered fields that were never offered together. The gateway
   bounds the shape of a response and the MCP re-checks policy at the point of
   mutation, but neither of them knows what this particular prompt offered —
   only the thing that built the prompt does, which is here.

3. **The response is the kind of answer this prompt asked for.** A radio
   response submitted against a prompt that asked a binary or text question is
   never valid, whatever its content, because it was never a possible answer
   to what was actually asked.

How it works without modifying NAT
----------------------------------
``FastApiFrontEndPluginWorker.__init__`` assigns ``self._execution_store``, so a
subclass can substitute its own store, and NAT's routes then close over ours.
Both hooks are ordinary method overrides:

* ``set_interaction_required`` runs inside the *workflow* task, which still
  carries the originating request's contextvars, so ``Context`` yields the
  gateway-injected identity of the user who is being asked. Owner and offered
  options are recorded there.
* ``resolve_interaction`` runs inside the *HTTP* task for the response, which
  has different contextvars. The responder's identity therefore comes from
  ``ResponderIdentityMiddleware``, a pure-ASGI middleware in this package that
  stashes it in a contextvar for the duration of that request.

Fail-closed, with one deliberate exception
------------------------------------------
An interaction this guard never saw created (NAT's own OAuth consent flow, for
instance) has no recorded owner. Those are allowed through and logged, because
refusing them would break a NAT feature this template does not otherwise touch.
Every interaction created by ``nat_streaming_react.approval`` *is* recorded, so
the approval path is never in that category. ``strict`` makes even the unknown
case fail closed, for a deployment that uses no other interaction type.
"""

import contextvars
import logging
import os
import typing
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any

from nat.front_ends.fastapi.execution_store import ExecutionStore
from nat.front_ends.fastapi.execution_store import PendingInteraction

logger = logging.getLogger(__name__)

#: Identity header the gateway injects. Never supplied by the browser or the
#: model: the gateway builds a fresh upstream request and forwards no client
#: headers.
IDENTITY_HEADER = "x-authenticated-user-id"

#: The authenticated user submitting the request currently being served.
_responder: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "nat_streaming_react_responder", default=None
)

#: Cancellation is part of the interaction protocol rather than any
#: application's vocabulary, so it is always an acceptable answer.
CANCEL_SENTINEL = "__CANCEL__"


def current_responder() -> str | None:
    """The authenticated user for the request being served, if known."""

    return _responder.get()


class InteractionAuthorizationError(Exception):
    """A response was not authorized for the interaction it targets."""


class ResponderIdentityMiddleware:
    """Record the authenticated caller for the duration of one request.

    Pure ASGI, like the other middleware in this package, so it cannot buffer a
    streamed response. It only reads a header and sets a contextvar.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[Any]],
        send: Callable[[Any], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        identity: str | None = None
        for name, value in scope.get("headers", ()):
            if name.lower() == IDENTITY_HEADER.encode("ascii"):
                identity = value.decode("latin-1").strip() or None
                break

        token = _responder.set(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            _responder.reset(token)


def _prompt_actor() -> str | None:
    """The authenticated user the workflow is currently serving.

    Read from NAT's request metadata, which the workflow task inherits from the
    request that started it, so it survives the interaction pause.
    """

    try:
        from nat.builder.context import Context

        headers = Context.get().metadata.headers
    except Exception:  # pragma: no cover - defensive; identity is optional here
        return None
    if not headers:
        return None
    value = headers.get(IDENTITY_HEADER)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _normalize_field(value: Any) -> str | None:
    """Normalize an option's id/value for comparison, preserving ``None``.

    Booleans (a binary prompt's option values are often ``True``/``False``)
    and every other scalar are compared as their string form, on both the
    offered side and the submitted side, so the two are always compared the
    same way.
    """

    return None if value is None else str(value)


class OfferedChoice(typing.NamedTuple):
    """One option's identity, exactly as offered or as submitted.

    A plain ``(id, value)`` pair rather than two independent sets: an offer is
    only satisfied by a submission that matches *both* fields from the *same*
    option, never by matching one field against one option and the other
    field against a different one.
    """

    id: str | None
    value: str | None


class PromptOffer(typing.NamedTuple):
    """What one pending prompt actually asked for."""

    #: Normalized ``input_type`` discriminator (e.g. ``"radio"``), or ``None``
    #: if the prompt object exposes none. A response whose own ``type`` field
    #: disagrees with this was never a possible answer to what was asked.
    prompt_type: str | None
    #: Offered ``(id, value)`` pairs, or ``None`` when the prompt is not
    #: choice-bearing (free text, notification, OAuth consent) and therefore
    #: has no option set to validate a response against.
    choices: frozenset[OfferedChoice] | None


def _prompt_type(prompt: Any) -> str | None:
    value = getattr(prompt, "input_type", None)
    return None if value is None else str(value)


def _response_type(response: Any) -> str | None:
    value = getattr(response, "type", None)
    return None if value is None else str(value)


def prompt_offer(prompt: Any) -> PromptOffer:
    """What ``prompt`` actually offered: its type, and its options if any."""

    options = getattr(prompt, "options", None)
    if not options:
        return PromptOffer(prompt_type=_prompt_type(prompt), choices=None)
    choices = frozenset(
        OfferedChoice(
            id=_normalize_field(getattr(option, "id", None)),
            value=_normalize_field(getattr(option, "value", None)),
        )
        for option in options
    )
    return PromptOffer(prompt_type=_prompt_type(prompt), choices=choices or None)


def submitted_choice(response: Any) -> OfferedChoice | None:
    """The ``(id, value)`` pair a response actually selected, if any."""

    selected = getattr(response, "selected_option", None)
    if selected is None:
        return None
    return OfferedChoice(
        id=_normalize_field(getattr(selected, "id", None)),
        value=_normalize_field(getattr(selected, "value", None)),
    )


def _strict_default() -> bool:
    raw = os.getenv("HITL_STRICT_INTERACTION_OWNERSHIP", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class OwnerAwareExecutionStore(ExecutionStore):
    """NAT's execution store, with an authorization check on response.

    Substituted for the stock store by
    ``fastapi_worker.AuthenticatedFastApiFrontEndPluginWorker``.
    """

    def __init__(self, *, strict: bool | None = None) -> None:
        super().__init__()
        self._owners: dict[str, str] = {}
        self._offers: dict[tuple[str, str], PromptOffer] = {}
        self._strict = _strict_default() if strict is None else strict

    async def set_interaction_required(
        self,
        execution_id: str,
        prompt: Any,
        interaction_id: str | None = None,
    ) -> PendingInteraction:
        pending = await super().set_interaction_required(execution_id, prompt, interaction_id)

        actor = _prompt_actor()
        if actor is not None:
            self._owners[execution_id] = actor
        else:
            logger.warning(
                "Interaction %s on execution %s was created with no authenticated "
                "identity; its response cannot be attributed to a user",
                pending.interaction_id,
                execution_id,
            )
        self._offers[(execution_id, pending.interaction_id)] = prompt_offer(prompt)
        return pending

    def authorize(self, execution_id: str, interaction_id: str, response: Any) -> None:
        """Raise unless this responder may answer this prompt with this value.

        Separated from :meth:`resolve_interaction` so the whole rejection matrix
        is testable without a running NAT, an event loop or a workflow.
        """

        responder = current_responder()
        owner = self._owners.get(execution_id)

        if owner is None:
            if self._strict:
                raise InteractionAuthorizationError(
                    "this interaction has no recorded owner and strict ownership is enabled"
                )
            logger.info(
                "Resolving interaction %s on execution %s with no recorded owner",
                interaction_id,
                execution_id,
            )
        elif responder is None:
            raise InteractionAuthorizationError(
                "an approval response must carry an authenticated identity"
            )
        elif responder != owner:
            # Deliberately not logged with both identities at warning level in a
            # way that implies which is legitimate: the point is only that they
            # differ.
            logger.warning(
                "Rejected an interaction response from a user who does not own "
                "execution %s",
                execution_id,
            )
            raise InteractionAuthorizationError(
                "this approval was not addressed to the authenticated user"
            )

        offer = self._offers.get((execution_id, interaction_id))
        if offer is None:
            # No offer was ever recorded for this interaction — a type this
            # guard did not see created (NAT's own OAuth consent flow; see the
            # module docstring). Nothing to validate a response shape against.
            return

        response_type = _response_type(response)
        if offer.prompt_type is not None and response_type is not None and response_type != offer.prompt_type:
            raise InteractionAuthorizationError(
                f"response type {response_type!r} does not match the pending "
                f"prompt type {offer.prompt_type!r}"
            )

        if offer.choices is None:
            # Free text, notification, or another non-choice-bearing prompt.
            # The type check above is the whole check for these.
            return

        submitted = submitted_choice(response)
        if submitted is None:
            raise InteractionAuthorizationError(
                "a choice-bearing approval requires a selected option"
            )

        # Cancellation is part of the interaction protocol, not any
        # application's vocabulary (see CANCEL_SENTINEL in the module
        # docstring), so it is accepted regardless of what this particular
        # prompt offered. But only when the *whole* selection is
        # self-consistently a cancellation: a value of CANCEL_SENTINEL paired
        # with a real, different id (or vice versa) is not a cancellation
        # that borrowed a spare field, it is an attempt to authorize that
        # other field's value by attaching it to a sentinel that always
        # passes.
        if submitted.value == CANCEL_SENTINEL and submitted.id in (None, CANCEL_SENTINEL):
            return

        if submitted not in offer.choices:
            raise InteractionAuthorizationError(
                "the submitted choice was not offered by this approval prompt"
            )

    async def resolve_interaction(
        self,
        execution_id: str,
        interaction_id: str,
        response: Any,
    ) -> None:
        self.authorize(execution_id, interaction_id, response)
        await super().resolve_interaction(execution_id, interaction_id, response)
        # The prompt is answered; forget what it offered. The owner entry is kept
        # until the execution is removed, because one execution can pause more
        # than once and every prompt in it belongs to the same user.
        self._offers.pop((execution_id, interaction_id), None)

    async def remove(self, execution_id: str) -> Any:
        self._owners.pop(execution_id, None)
        for key in [key for key in self._offers if key[0] == execution_id]:
            self._offers.pop(key, None)
        return await super().remove(execution_id)

    # -- test accessors --------------------------------------------------
    def record_owner_for_test(self, execution_id: str, actor: str) -> None:
        self._owners[execution_id] = actor

    def record_offer_for_test(
        self,
        execution_id: str,
        interaction_id: str,
        choices: frozenset[OfferedChoice] | None,
        *,
        prompt_type: str | None = None,
    ) -> None:
        self._offers[(execution_id, interaction_id)] = PromptOffer(prompt_type=prompt_type, choices=choices)
