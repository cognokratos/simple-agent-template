"""Offline tests for the agent half of the approval boundary.

Needs no cluster, no model and no database. The Rust half — signature
verification, binding, lifetime ceiling, the transition policy — is tested by
`cargo test -p tickets-mcp-server`; this covers what the agent is responsible for:

* the minted token's shape and its signature, verified against an independent
  reimplementation of the check, so the two sides cannot silently diverge;
* the canonical payload encoding both sides must agree on;
* which prompts a human is asked for, and when;
* the interaction-ownership and offered-choice checks that close NAT's
  two-UUIDs-is-authorization gap;
* that cancellation mints nothing at all.

Run inside the agent image::

    docker compose exec agent python /app/verify_approval_tokens.py
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import unittest

# setdefault is not enough: docker-compose passes HITL_APPROVAL_SECRET through as
# an *empty string* when the approval feature is off, so the key exists and
# setdefault leaves it empty. These tests mint tokens, so they need a real one.
if len(os.environ.get("HITL_APPROVAL_SECRET", "")) < 24:
    os.environ["HITL_APPROVAL_SECRET"] = "a-test-secret-of-at-least-24-characters"

from nat_streaming_react.approval import (  # noqa: E402
    ACTION_SET_TICKET_PRIORITY,
    CANCEL_SENTINEL,
    MAX_TOKEN_TTL_SECONDS,
    _note_disclosure,
    approval_result,
    approval_secret,
    build_claims,
    canonical_json,
    cancelled,
    execute_url,
    mint_token,
    model_supplied_note,
    needs_rationale,
    payload_hash,
    prompt_text,
    priority_options,
    SetTicketPriorityRequest,
)
from nat_streaming_react.interaction_guard import (  # noqa: E402
    InteractionAuthorizationError,
    OfferedChoice,
    OwnerAwareExecutionStore,
    _responder,
    prompt_offer,
    submitted_choice,
)

ACTOR = "support-rep-1"
REQUEST_ID = "11111111-1111-4111-8111-111111111111"
RESOURCE = "TKT-1001"


def verify_independently(secret: bytes, token: str) -> dict:
    """A second implementation of the check, written from the format alone.

    Deliberately not a call into the minter's own helpers: if this agrees with
    `mint_token`, the format is what both this file and the Rust verifier
    believe it is.
    """

    payload_b64, signature_b64 = token.split(".")

    def unpad(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    expected = hmac.new(secret, payload_b64.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, unpad(signature_b64)):
        raise AssertionError("signature does not verify")
    return json.loads(unpad(payload_b64))


def claims(**overrides) -> dict:
    base = dict(
        action=ACTION_SET_TICKET_PRIORITY,
        resource_id=RESOURCE,
        actor_id=ACTOR,
        request_id=REQUEST_ID,
        ttl_seconds=600,
        choice="high",
        expected_choice="medium",
        rationale="Reviewed with the customer's documentation.",
        payload={"note": "Supported by the invoice on file."},
    )
    base.update(overrides)
    return build_claims(**base)


class TokenShapeTests(unittest.TestCase):
    def test_a_minted_token_verifies_and_carries_every_binding(self):
        token = mint_token(approval_secret(), claims())
        decoded = verify_independently(approval_secret(), token)

        self.assertEqual(decoded["v"], 1)
        self.assertEqual(decoded["action"], ACTION_SET_TICKET_PRIORITY)
        self.assertEqual(decoded["resource_id"], RESOURCE)
        self.assertEqual(decoded["actor_id"], ACTOR)
        self.assertEqual(decoded["request_id"], REQUEST_ID)
        self.assertEqual(decoded["choice"], "high")
        self.assertEqual(decoded["expected_choice"], "medium")
        self.assertTrue(decoded["override_requested"])
        self.assertTrue(decoded["nonce"])

    def test_the_signature_covers_the_payload(self):
        token = mint_token(approval_secret(), claims())
        payload_b64, signature = token.split(".")
        tampered_payload = json.loads(
            base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
        )
        tampered_payload["resource_id"] = "TKT-1002"
        forged = (
            base64.urlsafe_b64encode(canonical_json(tampered_payload).encode())
            .decode()
            .rstrip("=")
            + "."
            + signature
        )
        with self.assertRaises(AssertionError):
            verify_independently(approval_secret(), forged)

    def test_a_different_secret_does_not_verify(self):
        token = mint_token(b"a-different-secret-also-24-chars+", claims())
        with self.assertRaises(AssertionError):
            verify_independently(approval_secret(), token)

    def test_every_nonce_is_unique(self):
        nonces = {claims()["nonce"] for _ in range(50)}
        self.assertEqual(len(nonces), 50, "a reused nonce would make an approval replayable")

    def test_the_expiry_is_bounded_by_the_configured_ttl(self):
        now = int(time.time())
        self.assertEqual(claims(ttl_seconds=600, **{})["exp"] - now, 600)
        for invalid in (0, 59, MAX_TOKEN_TTL_SECONDS + 1, 86_400):
            with self.subTest(ttl=invalid), self.assertRaises(ValueError):
                claims(ttl_seconds=invalid)

    def test_a_short_secret_is_refused(self):
        previous = os.environ["HITL_APPROVAL_SECRET"]
        os.environ["HITL_APPROVAL_SECRET"] = "too-short"
        try:
            with self.assertRaises(ValueError):
                approval_secret()
        finally:
            os.environ["HITL_APPROVAL_SECRET"] = previous

    def test_override_requested_is_derived_not_asserted(self):
        self.assertTrue(claims(choice="high", expected_choice="medium")["override_requested"])
        self.assertFalse(claims(choice="medium", expected_choice="medium")["override_requested"])


class CanonicalEncodingTests(unittest.TestCase):
    """Both sides hash the payload independently, in different languages."""

    def test_key_order_does_not_change_the_digest(self):
        self.assertEqual(
            payload_hash({"note": "n", "assignee": "a"}),
            payload_hash({"assignee": "a", "note": "n"}),
        )

    def test_content_does_change_the_digest(self):
        self.assertNotEqual(payload_hash({"note": "a"}), payload_hash({"note": "b"}))

    def test_the_digest_matches_the_payload_the_token_carries(self):
        built = claims()
        self.assertEqual(built["payload_sha256"], payload_hash(built["payload"]))

    def test_the_canonical_form_is_compact_and_sorted(self):
        self.assertEqual(canonical_json({"b": 1, "a": 2}), '{"a":2,"b":1}')
        # Pinned against the Rust side's canonical_json, which produces the same
        # bytes for the same value.
        self.assertEqual(
            payload_hash({"note": "x"}),
            hashlib.sha256(b'{"note":"x"}').hexdigest(),
        )


class PromptRuleTests(unittest.TestCase):
    def test_a_change_requires_a_reason_and_keeping_the_priority_does_not(self):
        self.assertTrue(needs_rationale("high", "medium"))
        self.assertTrue(needs_rationale("medium", "high"))
        self.assertFalse(needs_rationale("medium", "medium"))

    def test_every_allowed_priority_is_offered_plus_cancel(self):
        options = priority_options("medium", ("medium", "high"))
        ids = [option.id for option in options]
        self.assertEqual(ids, ["medium", "high", "cancel"])
        values = {option.id: option.value for option in options}
        self.assertEqual(values["cancel"], CANCEL_SENTINEL)
        keep = next(option for option in options if option.id == "medium")
        self.assertIn("Keep", keep.label)
        change = next(option for option in options if option.id == "high")
        self.assertIn("Change", change.label)
        self.assertIn("reason", change.description)

    def test_the_prompt_states_the_current_priority_and_the_proposal(self):
        request = SetTicketPriorityRequest(
            ticket_id=RESOURCE,
            current_priority="medium",
            requested_priority="high",
            summary="The customer has followed up twice with no resolution.",
        )
        note = model_supplied_note(request)
        text = prompt_text(request, note)
        self.assertIn(RESOURCE, text)
        self.assertIn("'medium'", text)
        self.assertIn("'high'", text)
        self.assertIn("requires a reason", text)
        # No note was supplied: no disclosure line, and an empty note still works.
        self.assertEqual(note, "")
        self.assertNotIn("Model-supplied note", text)

    def test_a_model_supplied_note_is_disclosed_before_approval(self):
        request = SetTicketPriorityRequest(
            ticket_id=RESOURCE,
            current_priority="medium",
            requested_priority="high",
            summary="s",
            note="  Documented via invoice #42, café receipt attached ☕  ",
        )
        note = model_supplied_note(request)
        self.assertEqual(note, "Documented via invoice #42, café receipt attached ☕")

        choice_prompt = prompt_text(request, note)
        self.assertIn("Model-supplied note", choice_prompt)
        self.assertIn(note, choice_prompt)

        rationale_prompt = "\n".join([
            f"Ticket {RESOURCE}: s",
            "irrelevant filler line",
            _note_disclosure(note),
        ])
        self.assertIn(note, rationale_prompt)
        self.assertIn("signed and recorded verbatim", _note_disclosure(note))

    def test_displayed_note_exactly_equals_the_persisted_note(self):
        """The same normalized value must be shown, signed and persisted.

        A hidden or substituted payload note — one that differs from what was
        displayed — would mean the human approved something other than what
        actually gets signed. Covers plain text, Unicode, embedded whitespace
        and the empty-note case together so none of them can drift apart.
        """

        for raw in (" plain note ", "unicode: café ☕", "line1\nline2\t indented", "", None):
            with self.subTest(raw=raw):
                request = SetTicketPriorityRequest(
                    ticket_id=RESOURCE,
                    current_priority="medium",
                    requested_priority="high",
                    summary="s",
                    note=raw,
                )
                note = model_supplied_note(request)
                displayed = prompt_text(request, note)
                persisted_payload = {"note": note} if note else {}

                if note:
                    self.assertIn(note, displayed)
                    self.assertEqual(persisted_payload["note"], note)
                    signed_claims = claims(payload=persisted_payload, choice="high")
                    self.assertEqual(signed_claims["payload"]["note"], note)
                    self.assertEqual(payload_hash(persisted_payload), payload_hash({"note": note}))
                else:
                    self.assertNotIn("Model-supplied note", displayed)
                    self.assertEqual(persisted_payload, {})

    def test_the_request_schema_rejects_an_unknown_status(self):
        for field in ("current_priority", "requested_priority"):
            payload = {
                "ticket_id": RESOURCE,
                "current_priority": "medium",
                "requested_priority": "high",
                "summary": "s",
            }
            payload[field] = "deleted"
            with self.subTest(field=field), self.assertRaises(Exception):
                SetTicketPriorityRequest(**payload)

    def test_the_request_schema_rejects_an_empty_identifier_or_summary(self):
        for field, value in (("ticket_id", ""), ("summary", "")):
            payload = {
                "ticket_id": RESOURCE,
                "current_priority": "medium",
                "requested_priority": "high",
                "summary": "s",
            }
            payload[field] = value
            with self.subTest(field=field), self.assertRaises(Exception):
                SetTicketPriorityRequest(**payload)


class ModelFacingResultTests(unittest.TestCase):
    """An approval is not an outcome; the model must not be told otherwise."""

    def test_a_refused_change_tells_the_model_nothing_was_applied(self):
        result = json.loads(
            approval_result(
                resource_id=RESOURCE,
                action=ACTION_SET_TICKET_PRIORITY,
                request_id=REQUEST_ID,
                committed=False,
                result={"refused": "the ticket is already high priority"},
            )
        )
        self.assertFalse(result["committed"])
        self.assertIn("NOTHING was applied", result["next_step"])
        self.assertIn("never", result["next_step"])

    def test_an_applied_change_is_reported_as_already_applied(self):
        result = json.loads(
            approval_result(
                resource_id=RESOURCE,
                action=ACTION_SET_TICKET_PRIORITY,
                request_id=REQUEST_ID,
                committed=True,
                result={"new_priority": "high"},
            )
        )
        self.assertTrue(result["committed"])
        self.assertIn("already applied", result["next_step"])

    def test_a_cancellation_is_reported_as_unapproved(self):
        result = json.loads(cancelled(RESOURCE, ACTION_SET_TICKET_PRIORITY, "The user cancelled."))
        self.assertFalse(result["approved"])
        self.assertNotIn("approval_token", result)


class ExecutionEndpointTests(unittest.TestCase):
    def test_the_execution_url_is_derived_from_the_mcp_url(self):
        previous = os.environ.get("TICKETS_MCP_URL")
        try:
            os.environ["TICKETS_MCP_URL"] = "http://mcp-server:8080/mcp"
            self.assertEqual(execute_url(), "http://mcp-server:8080/approvals/execute")
            os.environ["TICKETS_MCP_URL"] = "http://mcp-server:8080/mcp/"
            self.assertEqual(execute_url(), "http://mcp-server:8080/approvals/execute")
            os.environ.pop("TICKETS_MCP_URL")
            self.assertIsNone(execute_url())
        finally:
            if previous is None:
                os.environ.pop("TICKETS_MCP_URL", None)
            else:
                os.environ["TICKETS_MCP_URL"] = previous


class _Option:
    def __init__(self, identifier, value):
        self.id = identifier
        self.value = value


class _Prompt:
    def __init__(self, options, input_type="radio"):
        self.options = options
        self.input_type = input_type


class _Response:
    def __init__(self, selected_option, response_type="radio"):
        self.selected_option = selected_option
        self.type = response_type


class InteractionAuthorizationTests(unittest.TestCase):
    """The gap in stock NAT: two UUIDs are not authorization.

    NAT's endpoint calls resolve_interaction with no notion of who is asking and
    no notion of what the prompt offered. These assert both halves, plus that
    an offered id and an offered value only authorize a response when they came
    from the *same* offered option, not from anywhere in the offered set.
    """

    EXECUTION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    INTERACTION = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    #: Deliberately asymmetric ids and values, so a bug that checks either
    #: field independently against the whole offered set (rather than the
    #: matching pair) is caught by a mismatched cross-reference.
    OPTIONS = [
        _Option("medium", "MEDIUM"),
        _Option("high", "HIGH"),
        _Option("cancel", CANCEL_SENTINEL),
    ]

    def setUp(self):
        self.store = OwnerAwareExecutionStore(strict=False)
        self.store.record_owner_for_test(self.EXECUTION, ACTOR)
        self.store.record_offer_for_test(
            self.EXECUTION,
            self.INTERACTION,
            prompt_offer(_Prompt(self.OPTIONS)).choices,
            prompt_type="radio",
        )
        self._token = _responder.set(ACTOR)

    def tearDown(self):
        _responder.reset(self._token)

    def _authorize(self, response):
        self.store.authorize(self.EXECUTION, self.INTERACTION, response)

    def test_the_owner_may_answer_with_an_offered_choice(self):
        self._authorize(_Response(_Option("medium", "MEDIUM")))
        self._authorize(_Response(_Option("high", "HIGH")))

    def test_cancellation_is_always_acceptable(self):
        self._authorize(_Response(_Option("cancel", CANCEL_SENTINEL)))
        # Cancellation is protocol-level, not application vocabulary: it is
        # accepted even with an id/value pair this specific prompt never
        # declared, as long as the whole selection is self-consistently a
        # cancellation.
        self._authorize(_Response(_Option(CANCEL_SENTINEL, CANCEL_SENTINEL)))
        self._authorize(_Response(_Option(None, CANCEL_SENTINEL)))

    def test_another_authenticated_user_may_not_answer(self):
        _responder.set("support-rep-2")
        with self.assertRaises(InteractionAuthorizationError) as raised:
            self._authorize(_Response(_Option("high", "HIGH")))
        self.assertIn("not addressed to the authenticated user", str(raised.exception))

    def test_an_unauthenticated_response_is_refused(self):
        _responder.set(None)
        with self.assertRaises(InteractionAuthorizationError) as raised:
            self._authorize(_Response(_Option("high", "HIGH")))
        self.assertIn("authenticated identity", str(raised.exception))

    def test_a_choice_the_prompt_never_offered_is_refused(self):
        for identifier, value in (
            ("deleted", "DELETED"),
            ("escalated", "ESCALATED"),
            ("", ""),
            ("medium", "medium"),  # right id, wrong (lowercased) value
        ):
            with self.subTest(identifier=identifier, value=value):
                with self.assertRaises(InteractionAuthorizationError) as raised:
                    self._authorize(_Response(_Option(identifier, value)))
                self.assertIn("not offered", str(raised.exception))

    def test_offered_id_with_unoffered_value_is_refused(self):
        """A valid id does not license an arbitrary value on that option."""

        with self.assertRaises(InteractionAuthorizationError) as raised:
            self._authorize(_Response(_Option("medium", "unoffered-value")))
        self.assertIn("not offered", str(raised.exception))

    def test_offered_value_with_unoffered_id_is_refused(self):
        """A valid value does not license an arbitrary id on that option."""

        with self.assertRaises(InteractionAuthorizationError) as raised:
            self._authorize(_Response(_Option("unoffered-id", "MEDIUM")))
        self.assertIn("not offered", str(raised.exception))

    def test_two_individually_offered_but_mismatched_fields_are_refused(self):
        """id from one option plus value from another must not authorize."""

        with self.assertRaises(InteractionAuthorizationError) as raised:
            self._authorize(_Response(_Option("high", "MEDIUM")))
        self.assertIn("not offered", str(raised.exception))

    def test_cancel_id_with_a_real_action_value_is_refused(self):
        """The cancel sentinel on one field must not authorize a real value on
        the other: cancellation is only recognised when the *whole* selection
        is self-consistently a cancellation."""

        with self.assertRaises(InteractionAuthorizationError) as raised:
            self._authorize(_Response(_Option(CANCEL_SENTINEL, "MEDIUM")))
        self.assertIn("not offered", str(raised.exception))

    def test_cancel_value_with_an_unoffered_real_id_is_refused(self):
        with self.assertRaises(InteractionAuthorizationError) as raised:
            self._authorize(_Response(_Option("unoffered-id", CANCEL_SENTINEL)))
        self.assertIn("not offered", str(raised.exception))

    def test_wrong_response_type_is_refused(self):
        """A response shaped for a different prompt type is never valid, even
        if its selected_option happens to collide with an offered pair."""

        with self.assertRaises(InteractionAuthorizationError) as raised:
            self._authorize(_Response(_Option("medium", "MEDIUM"), response_type="dropdown"))
        self.assertIn("does not match the pending prompt type", str(raised.exception))

    def test_a_choice_bearing_prompt_requires_a_selection(self):
        with self.assertRaises(InteractionAuthorizationError):
            self._authorize(_Response(None))

    def test_a_free_text_prompt_has_no_choice_set_to_validate(self):
        self.store.record_offer_for_test(
            self.EXECUTION, self.INTERACTION, None, prompt_type="text"
        )
        self._authorize(_Response(None, response_type="text"))

    def test_a_free_text_prompt_still_rejects_the_wrong_response_type(self):
        self.store.record_offer_for_test(
            self.EXECUTION, self.INTERACTION, None, prompt_type="text"
        )
        with self.assertRaises(InteractionAuthorizationError) as raised:
            self._authorize(_Response(_Option("medium", "MEDIUM"), response_type="radio"))
        self.assertIn("does not match the pending prompt type", str(raised.exception))

    def test_binary_options_are_validated_not_skipped(self):
        """Boolean-valued options must still be checked, not waved through."""

        self.store.record_offer_for_test(
            self.EXECUTION,
            self.INTERACTION,
            prompt_offer(
                _Prompt([_Option("confirm", True), _Option("deny", False)], input_type="binary_choice")
            ).choices,
            prompt_type="binary_choice",
        )
        self._authorize(_Response(_Option("confirm", True), response_type="binary_choice"))
        with self.assertRaises(InteractionAuthorizationError) as raised:
            self._authorize(_Response(_Option("confirm", False), response_type="binary_choice"))
        self.assertIn("not offered", str(raised.exception))

    def test_rejection_does_not_consume_the_pending_interaction(self):
        """A rejected submission must leave the legitimate offer still usable."""

        with self.assertRaises(InteractionAuthorizationError):
            self._authorize(_Response(_Option("medium", "unoffered-value")))
        # The offer is still there, and a legitimate choice still succeeds.
        self._authorize(_Response(_Option("medium", "MEDIUM")))

    def test_an_unowned_interaction_is_allowed_unless_strict(self):
        lenient = OwnerAwareExecutionStore(strict=False)
        lenient.authorize("unknown-execution", "unknown-interaction", _Response(None))

        strict = OwnerAwareExecutionStore(strict=True)
        with self.assertRaises(InteractionAuthorizationError) as raised:
            strict.authorize("unknown-execution", "unknown-interaction", _Response(None))
        self.assertIn("no recorded owner", str(raised.exception))

    def test_prompt_offer_pairs_id_and_value_from_the_same_option(self):
        offer = prompt_offer(_Prompt(self.OPTIONS))
        self.assertIsNotNone(offer.choices)
        self.assertIn(OfferedChoice(id="medium", value="MEDIUM"), offer.choices)
        self.assertIn(OfferedChoice(id="cancel", value=CANCEL_SENTINEL), offer.choices)
        # The flattened-union bug this replaces would also accept this cross
        # pairing; the structured offer must not contain it.
        self.assertNotIn(OfferedChoice(id="medium", value="HIGH"), offer.choices)

        binary_offer = prompt_offer(_Prompt([_Option("confirm", True), _Option("deny", False)]))
        self.assertIsNotNone(binary_offer.choices)
        self.assertIn(OfferedChoice(id="confirm", value="True"), binary_offer.choices)

        text_offer = prompt_offer(_Prompt(None, input_type="text"))
        self.assertIsNone(text_offer.choices)
        self.assertEqual(text_offer.prompt_type, "text")

    def test_submitted_choice_reads_id_and_value_as_one_pair(self):
        self.assertEqual(submitted_choice(_Response(_Option("medium", "MEDIUM"))), OfferedChoice("medium", "MEDIUM"))
        self.assertIsNone(submitted_choice(_Response(None)))


class RealExecutionStoreRoundTripTests(unittest.IsolatedAsyncioTestCase):
    """Drive the actual NAT ``ExecutionStore`` path, not test-only accessors.

    ``record_owner_for_test``/``record_offer_for_test`` exist so the rejection
    matrix above is testable without an event loop, but they do not prove that
    a *real* ``set_interaction_required`` call records an offer this guard can
    later check, or that a *real* ``resolve_interaction`` call — the one NAT's
    own HTTP route calls — actually resolves the pending interaction's future.
    """

    async def test_a_real_prompt_is_recorded_and_a_valid_response_resolves_it(self):
        store = OwnerAwareExecutionStore(strict=False)
        record = await store.create_execution()
        store.record_owner_for_test(record.execution_id, ACTOR)

        prompt = _Prompt([_Option("medium", "MEDIUM"), _Option("high", "HIGH")])
        pending = await store.set_interaction_required(record.execution_id, prompt)

        token = _responder.set(ACTOR)
        try:
            response = _Response(_Option("high", "HIGH"))
            await store.resolve_interaction(record.execution_id, pending.interaction_id, response)
        finally:
            _responder.reset(token)

        self.assertTrue(pending.future.done())
        self.assertIs(pending.future.result(), response)

    async def test_a_rejected_response_leaves_the_real_pending_future_unresolved(self):
        store = OwnerAwareExecutionStore(strict=False)
        record = await store.create_execution()
        store.record_owner_for_test(record.execution_id, ACTOR)

        prompt = _Prompt([_Option("medium", "MEDIUM"), _Option("high", "HIGH")])
        pending = await store.set_interaction_required(record.execution_id, prompt)

        token = _responder.set(ACTOR)
        try:
            bad_response = _Response(_Option("medium", "unoffered-value"))
            with self.assertRaises(InteractionAuthorizationError):
                await store.resolve_interaction(record.execution_id, pending.interaction_id, bad_response)
            self.assertFalse(pending.future.done())

            # The legitimate interaction is still usable after the rejection.
            good_response = _Response(_Option("medium", "MEDIUM"))
            await store.resolve_interaction(record.execution_id, pending.interaction_id, good_response)
        finally:
            _responder.reset(token)

        self.assertTrue(pending.future.done())
        self.assertIs(pending.future.result(), good_response)

    async def test_another_users_response_is_rejected_through_the_real_store(self):
        store = OwnerAwareExecutionStore(strict=False)
        record = await store.create_execution()
        store.record_owner_for_test(record.execution_id, ACTOR)

        prompt = _Prompt([_Option("medium", "MEDIUM"), _Option("high", "HIGH")])
        pending = await store.set_interaction_required(record.execution_id, prompt)

        token = _responder.set("support-rep-2")
        try:
            with self.assertRaises(InteractionAuthorizationError):
                await store.resolve_interaction(
                    record.execution_id, pending.interaction_id, _Response(_Option("medium", "MEDIUM"))
                )
        finally:
            _responder.reset(token)
        self.assertFalse(pending.future.done())


if __name__ == "__main__":
    unittest.main(verbosity=2)
