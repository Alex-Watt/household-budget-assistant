import json
from dataclasses import replace

import pytest

from budget_bot.discord import MockDiscord
from budget_bot.messaging import process
from budget_bot.models import Question
from budget_bot.ports import MessageInProgress
from budget_bot.worker import handle
from tests.helpers import (
    ALICE,
    BOB,
    INTERACTION_TOKEN,
    STRANGER,
    FixedResponder,
    MemoryState,
    settings,
)


def question(interaction_id: str = "300", user: str = ALICE, text: str = "Hi") -> Question:
    return Question(interaction_id, user, text, INTERACTION_TOKEN)


def test_duplicate_and_dry_run_never_ask_the_responder() -> None:
    state, discord, responder = MemoryState(), MockDiscord(), FixedResponder()
    assert process(question(), state, discord, settings(), responder) == "dry_run"
    assert process(question(), state, discord, settings(), responder) == "duplicate"
    assert not discord.replies and not responder.calls


def test_live_reply_edits_the_deferred_message_with_the_responders_answer() -> None:
    discord, responder = MockDiscord(), FixedResponder("You have $5.00 left.")
    outcome = process(
        question(user=BOB, text="help"), MemoryState(), discord, settings(True), responder
    )
    assert outcome == "sent"
    assert discord.replies == [(INTERACTION_TOKEN, "You have $5.00 left.")]
    assert responder.calls == [("help", BOB)]


def test_failed_reply_can_retry() -> None:
    class FailingDiscord(MockDiscord):
        def edit_reply(self, token: str, text: str) -> None:
            raise RuntimeError("Discord unavailable")

    state = MemoryState()
    with pytest.raises(RuntimeError):
        process(question(), state, FailingDiscord(), settings(True), FixedResponder())
    assert not state.claims and not state.completed
    assert process(question(), state, MockDiscord(), settings(True), FixedResponder()) == "sent"


def test_active_claim_is_not_acknowledged_as_duplicate() -> None:
    state = MemoryState()
    state.claim("301")
    with pytest.raises(MessageInProgress):
        process(question("301"), state, MockDiscord(), settings(True), FixedResponder())


def test_unauthorized_queue_sender_has_no_side_effects() -> None:
    state, discord = MemoryState(), MockDiscord()
    with pytest.raises(ValueError, match="unauthorized"):
        process(question(user=STRANGER), state, discord, settings(True), FixedResponder())
    assert not state.claims and not discord.replies


def test_partial_batch_retries_only_failed_records(caplog: pytest.LogCaptureFixture) -> None:
    state = MemoryState()
    ok = question("302", text="sensitive question text")
    event = {
        "Records": [
            {"messageId": "sqs-ok", "body": json.dumps(ok.to_dict())},
            {"messageId": "sqs-bad", "body": "invalid json"},
            {
                "messageId": "sqs-busy",
                "body": json.dumps(replace(ok, interaction_id="9").to_dict()),
            },
        ]
    }
    state.claim("9")
    assert handle(event, state, MockDiscord(), settings(), FixedResponder()) == {
        "batchItemFailures": [{"itemIdentifier": "sqs-bad"}, {"itemIdentifier": "sqs-busy"}]
    }
    assert state.completed == {"302"}
    for private in (ok.text, ALICE, INTERACTION_TOKEN):
        assert private not in caplog.text


def test_responder_failure_releases_the_claim_so_sqs_can_retry() -> None:
    class BrokenResponder:
        def answer(self, text: str, user_id: str, request_id: str = "") -> str:
            raise RuntimeError("model exploded")

    state, discord = MemoryState(), MockDiscord()
    with pytest.raises(RuntimeError):
        process(question(), state, discord, settings(True), BrokenResponder())
    assert not state.claims and not state.completed and not discord.replies
