from typing import Any

from botocore.exceptions import ClientError

from scripts.check_ai_access import probe
from tests.unit.test_llm import PROFILE_IDS, FakeControl, FakeRuntime, answer, error


def test_probe_reports_each_denied_model_then_the_first_that_works() -> None:
    runtime = FakeRuntime(
        {
            "us.anthropic.claude-sonnet-5-5": error("AccessDeniedException"),
            "us.anthropic.claude-sonnet-5": error("AccessDeniedException"),
            "us.anthropic.claude-sonnet-4-6": answer("ok"),
        }
    )
    assert probe(runtime, FakeControl(PROFILE_IDS), "us") == [
        ("us.anthropic.claude-sonnet-5-5", "AccessDeniedException"),
        ("us.anthropic.claude-sonnet-5", "AccessDeniedException"),
        ("us.anthropic.claude-sonnet-4-6", "works"),
    ]
    # It stops at the first working model, so a probe costs as little as possible.
    assert len(runtime.calls) == 3
    assert all(call["inferenceConfig"] == {"maxTokens": 16} for call in runtime.calls)


def test_probe_with_nothing_usable_lists_every_failure() -> None:
    form: Any = ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "Converse")
    runtime = FakeRuntime({m: form for m in PROFILE_IDS})
    results = probe(runtime, FakeControl(PROFILE_IDS), "us")
    assert len(results) == 5 and all(result == "ResourceNotFoundException" for _, result in results)


def test_probe_with_no_sonnet_profiles_is_empty() -> None:
    assert probe(FakeRuntime({}), FakeControl(["us.anthropic.claude-haiku-5-5"]), "us") == []
