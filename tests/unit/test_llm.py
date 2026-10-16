from typing import Any

import pytest
from botocore.exceptions import ClientError

from budget_bot.llm import REFRESH_SECONDS, BedrockSonnet, LlmUnavailable, sonnet_candidates

# Profile IDs as returned by Bedrock in this project's selected Region (abridged).
PROFILE_IDS = [
    "us.anthropic.claude-3-haiku-20240307-v1:0",
    "us.anthropic.claude-sonnet-4-20250514-v1:0",
    "global.anthropic.claude-sonnet-4-20250514-v1:0",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.anthropic.claude-sonnet-4-6",
    "global.anthropic.claude-sonnet-4-6",
    "us.anthropic.claude-sonnet-5",
    "us.anthropic.claude-sonnet-5-5",
    "global.anthropic.claude-sonnet-5-5",
    "us.anthropic.claude-haiku-5-5",
    "us.anthropic.claude-opus-5-5",
    "us.anthropic.claude-fable-5-1",
    "eu.anthropic.claude-sonnet-9",
]


def error(code: str, message: str = "") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "Converse")


def answer(text: str = "hi", stop: str = "end_turn") -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": stop,
    }


class FakeRuntime:
    def __init__(self, outcomes: dict[str, Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        outcome = self.outcomes[kwargs["modelId"]]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeControl:
    def __init__(self, ids: list[str], pages: int = 1) -> None:
        self.ids = ids
        self.pages = pages
        self.calls = 0

    def list_inference_profiles(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        assert kwargs["typeEquals"] == "SYSTEM_DEFINED"
        size = -(-len(self.ids) // self.pages)
        start = int(kwargs.get("nextToken", "0"))
        page: dict[str, Any] = {
            "inferenceProfileSummaries": [
                {"inferenceProfileId": i, "status": "ACTIVE"}
                for i in self.ids[start : start + size]
            ]
        }
        if start + size < len(self.ids):
            page["nextToken"] = str(start + size)
        return page


def test_highest_sonnet_first_and_nothing_but_sonnet() -> None:
    assert sonnet_candidates(PROFILE_IDS, "us") == [
        "us.anthropic.claude-sonnet-5-5",
        "us.anthropic.claude-sonnet-5",
        "us.anthropic.claude-sonnet-4-6",
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "us.anthropic.claude-sonnet-4-20250514-v1:0",
    ]
    assert sonnet_candidates(PROFILE_IDS, "global")[0] == "global.anthropic.claude-sonnet-5-5"
    # Higher-numbered Opus, Haiku and Fable models are never chosen: the household asked for Sonnet.
    assert not any(
        word in profile
        for profile in sonnet_candidates(PROFILE_IDS, "us")
        for word in ("opus", "haiku", "fable")
    )


def test_a_future_sonnet_is_picked_up_without_a_code_change() -> None:
    future = [*PROFILE_IDS, "us.anthropic.claude-sonnet-5-10", "us.anthropic.claude-sonnet-6"]
    assert sonnet_candidates(future, "us")[:3] == [
        "us.anthropic.claude-sonnet-6",
        "us.anthropic.claude-sonnet-5-10",
        "us.anthropic.claude-sonnet-5-5",
    ]


def test_uses_the_newest_model_and_always_sets_max_tokens() -> None:
    runtime = FakeRuntime({"us.anthropic.claude-sonnet-5-5": answer("Done.")})
    model = BedrockSonnet(runtime, FakeControl(PROFILE_IDS, pages=3))
    reply = model.converse("system text", [{"role": "user", "content": [{"text": "q"}]}], [], 700)
    assert (reply.text, reply.model_id) == ("Done.", "us.anthropic.claude-sonnet-5-5")
    call = runtime.calls[0]
    assert call["inferenceConfig"] == {"maxTokens": 700}
    assert call["system"] == [{"text": "system text"}]
    assert "temperature" not in str(call)


def test_falls_back_to_the_highest_model_the_account_can_use_and_remembers_it() -> None:
    runtime = FakeRuntime(
        {
            "us.anthropic.claude-sonnet-5-5": error(
                "AccessDeniedException",
                "anthropic.claude-sonnet-5-5 is not available for this account.",
            ),
            "us.anthropic.claude-sonnet-5": error("AccessDeniedException"),
            "us.anthropic.claude-sonnet-4-6": answer("From 4.6"),
        }
    )
    model = BedrockSonnet(runtime, FakeControl(PROFILE_IDS))
    assert model.converse("s", [], [], 100).model_id == "us.anthropic.claude-sonnet-4-6"
    assert len(runtime.calls) == 3
    # The next question goes straight to the working model.
    assert model.converse("s", [], [], 100).text == "From 4.6"
    assert len(runtime.calls) == 4


def test_first_use_form_not_yet_submitted_also_falls_through() -> None:
    form = error("ResourceNotFoundException", "Model use case details have not been submitted")
    runtime = FakeRuntime({model_id: form for model_id in sonnet_candidates(PROFILE_IDS, "us")})
    with pytest.raises(LlmUnavailable):
        BedrockSonnet(runtime, FakeControl(PROFILE_IDS)).converse("s", [], [], 100)


def test_denied_models_are_retried_after_the_refresh_interval() -> None:
    clock = [0.0]
    runtime = FakeRuntime(
        {
            "us.anthropic.claude-sonnet-5-5": error("AccessDeniedException"),
            "us.anthropic.claude-sonnet-5": answer("From 5"),
        }
    )
    model = BedrockSonnet(runtime, FakeControl(PROFILE_IDS), clock=lambda: clock[0])
    assert model.converse("s", [], [], 100).model_id == "us.anthropic.claude-sonnet-5"
    # The household fixes access; once the cache expires the newest model is tried again.
    runtime.outcomes["us.anthropic.claude-sonnet-5-5"] = answer("From 5.5")
    clock[0] = REFRESH_SECONDS + 1
    # No manual reset: a warm runtime must move up on its own once the newest model works.
    assert model.converse("s", [], [], 100).model_id == "us.anthropic.claude-sonnet-5-5"


def test_throttling_and_other_errors_are_not_treated_as_missing_access() -> None:
    runtime = FakeRuntime({"us.anthropic.claude-sonnet-5-5": error("ThrottlingException")})
    with pytest.raises(ClientError):
        BedrockSonnet(runtime, FakeControl(PROFILE_IDS)).converse("s", [], [], 100)
    assert len(runtime.calls) == 1


DAILY = "Too many tokens per day, please wait before trying again."


def test_a_used_up_daily_token_allowance_stops_instead_of_trying_another_model() -> None:
    runtime = FakeRuntime(
        {
            "us.anthropic.claude-sonnet-5-5": error("ThrottlingException", DAILY),
            "us.anthropic.claude-sonnet-4-6": answer("From 4.6"),
        }
    )
    with pytest.raises(ClientError):
        BedrockSonnet(runtime, FakeControl(PROFILE_IDS)).converse("s", [], [], 100)
    assert len(runtime.calls) == 1


@pytest.mark.parametrize(
    "message",
    ["", "Too many requests, please wait before trying again.", "Too many tokens, please wait."],
)
def test_ordinary_per_minute_throttling_never_downgrades_the_model(message: str) -> None:
    runtime = FakeRuntime({"us.anthropic.claude-sonnet-5-5": error("ThrottlingException", message)})
    model = BedrockSonnet(runtime, FakeControl(PROFILE_IDS))
    with pytest.raises(ClientError):
        model.converse("s", [], [], 100)
    assert len(runtime.calls) == 1 and not model._unusable


def test_validation_errors_that_are_our_fault_are_not_swallowed() -> None:
    runtime = FakeRuntime(
        {"us.anthropic.claude-sonnet-5-5": error("ValidationException", "bad toolConfig")}
    )
    with pytest.raises(ClientError):
        BedrockSonnet(runtime, FakeControl(PROFILE_IDS)).converse("s", [], [], 100)


def test_on_demand_not_supported_moves_to_the_next_model() -> None:
    runtime = FakeRuntime(
        {
            "us.anthropic.claude-sonnet-5-5": error(
                "ValidationException", "Invocation with on-demand throughput isn't supported"
            ),
            "us.anthropic.claude-sonnet-5": answer("ok"),
        }
    )
    assert BedrockSonnet(runtime, FakeControl(PROFILE_IDS)).converse("s", [], [], 1).text == "ok"


def test_no_sonnet_at_all_is_unavailable() -> None:
    with pytest.raises(LlmUnavailable):
        BedrockSonnet(FakeRuntime({}), FakeControl(["us.anthropic.claude-haiku-5-5"])).converse(
            "s", [], [], 100
        )


def test_tool_use_blocks_are_parsed() -> None:
    response = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"text": "Let me look."},
                    {
                        "toolUse": {
                            "toolUseId": "abc",
                            "name": "month_summary",
                            "input": {"month": "2026-10"},
                        }
                    },
                ],
            }
        },
        "stopReason": "tool_use",
    }
    runtime = FakeRuntime({"us.anthropic.claude-sonnet-5-5": response})
    reply = BedrockSonnet(runtime, FakeControl(PROFILE_IDS)).converse("s", [], [], 100)
    assert [(c.tool_use_id, c.name, c.arguments) for c in reply.tool_calls] == [
        ("abc", "month_summary", {"month": "2026-10"})
    ]
    assert reply.message == response["output"]["message"]


def test_a_truncated_answer_is_rejected() -> None:
    runtime = FakeRuntime(
        {"us.anthropic.claude-sonnet-5-5": answer("The total is $1", "max_tokens")}
    )
    with pytest.raises(LlmUnavailable):
        BedrockSonnet(runtime, FakeControl(PROFILE_IDS)).converse("s", [], [], 100)
