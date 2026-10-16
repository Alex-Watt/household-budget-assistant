from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from scripts import deploy
from scripts.deploy import DeployError, apply_changes, describe, main, parse, read_live

LIVE_ENV = {
    "DISCORD_APPLICATION_ID": "100000000000000001",
    "DISCORD_PUBLIC_KEY": "ab" * 32,
    "DISCORD_GUILD_ID": "100000000000000002",
    "DISCORD_CHANNEL_ID": "100000000000000003",
    "DISCORD_MEMBER_1_ID": "100000000000000004",
    "DISCORD_SECRET_NAME": "household-budget/discord",
    "LIVE_MESSAGING_ENABLED": "true",
    "AI_ENABLED": "false",
    "PLAID_ENABLED": "false",
    "SOMETHING_ELSE": "ignored",
}


class FakeAws:
    """Answers the handful of AWS CLI calls the script makes."""

    def __init__(
        self,
        env: dict[str, str] | None = None,
        outputs: dict[str, str] | None = None,
        logical: str = "Worker11F36D0F",
    ) -> None:
        self.env = LIVE_ENV if env is None else env
        self.outputs = outputs or {}
        self.logical = logical
        self.calls: list[Sequence[str]] = []

    def __call__(self, args: Sequence[str]) -> Any:
        self.calls.append(args)
        if args[0] == "sts":
            return {"Account": "123456789012"}
        if args[1] == "describe-stacks":
            return {
                "Stacks": [
                    {
                        "Outputs": [
                            {"OutputKey": k, "OutputValue": v} for k, v in self.outputs.items()
                        ]
                    }
                ]
            }
        if args[1] == "list-stack-resources":
            return {
                "StackResourceSummaries": [
                    {
                        "ResourceType": "AWS::SQS::Queue",
                        "LogicalResourceId": "WorkerQueue",
                        "PhysicalResourceId": "q",
                    },
                    {
                        "ResourceType": "AWS::Lambda::Function",
                        "LogicalResourceId": self.logical,
                        "PhysicalResourceId": "worker-fn",
                    },
                ]
            }
        assert args[1] == "get-function-configuration" and args[-1] == "worker-fn"
        return {"Environment": {"Variables": self.env}}


class Recorder:
    def __init__(self) -> None:
        self.commands: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, command: Sequence[str], env: Mapping[str, str]) -> None:
        self.commands.append((list(command), dict(env)))

    def verbs(self) -> list[str]:
        return ["build" if "build_lambda.py" in c[-1] else c[3] for c, _ in self.commands]


def run(
    argv: list[str], aws: FakeAws | None = None, answer: str = "y"
) -> tuple[int, Recorder, list[str]]:
    recorder, said = Recorder(), []
    code = main(argv, aws or FakeAws(), recorder, lambda _prompt: answer, said.append)
    return code, recorder, said


def test_live_settings_are_read_from_the_running_worker_and_stack_outputs() -> None:
    aws = FakeAws(
        outputs={"AlertEmail": "me@example.com", "MonthlyCostAlertUsd": "75", "Other": "x"}
    )
    live = read_live(aws)
    assert live["DISCORD_APPLICATION_ID"] == "100000000000000001"
    assert live["LIVE_MESSAGING_ENABLED"] == "true" and "SOMETHING_ELSE" not in live
    assert (live["ALERT_EMAIL"], live["MONTHLY_COST_ALERT_USD"]) == ("me@example.com", "75")
    assert "ALERT_EMAIL" not in read_live(FakeAws())


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"logical": "ReportAbc"}, "no worker function"),
        (
            {"env": {k: v for k, v in LIVE_ENV.items() if k != "DISCORD_GUILD_ID"}},
            "DISCORD_GUILD_ID",
        ),
        (
            {"env": {k: v for k, v in LIVE_ENV.items() if k != "LIVE_MESSAGING_ENABLED"}},
            "LIVE_MESSAGING_ENABLED",
        ),
    ],
)
def test_an_unreadable_or_incomplete_stack_is_never_replaced_with_defaults(
    kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(DeployError, match=message):
        read_live(FakeAws(**kwargs))
    code, recorder, said = run([], FakeAws(**kwargs))
    assert code == 2 and not recorder.commands and "Stopped" in said[0]


def test_a_missing_stack_stops_before_building_anything() -> None:
    def broken(args: Sequence[str]) -> Any:
        return {}

    recorder, said = Recorder(), []
    assert main([], broken, recorder, lambda _p: "y", said.append) == 2
    assert not recorder.commands and "Is it deployed" in said[0]


def test_nothing_changes_unless_a_flag_asks_for_it() -> None:
    live = read_live(FakeAws())
    assert apply_changes(live, parse([])) == live
    new = apply_changes(live, parse(["--plaid", "on", "--alert-email", "me@example.com"]))
    assert new["PLAID_ENABLED"] == "true" and new["AI_ENABLED"] == "false"
    assert new["LIVE_MESSAGING_ENABLED"] == "true" and new["ALERT_EMAIL"] == "me@example.com"
    assert apply_changes(new, parse(["--alert-email", "none"])).get("ALERT_EMAIL") is None
    assert apply_changes(live, parse(["--live", "off"]))["LIVE_MESSAGING_ENABLED"] == "false"


def test_a_missing_feature_flag_means_off_not_an_error() -> None:
    sparse = {k: v for k, v in LIVE_ENV.items() if k not in ("AI_ENABLED", "PLAID_ENABLED")}
    settings = apply_changes(read_live(FakeAws(env=sparse)), parse([]))
    assert settings["AI_ENABLED"] == settings["PLAID_ENABLED"] == "false"


@pytest.mark.parametrize("argv", [["--alert-email", "not-an-email"], ["--monthly-budget", "0"]])
def test_invalid_settings_stop_before_anything_runs(argv: list[str]) -> None:
    code, recorder, said = run(argv)
    assert code == 2 and not recorder.commands and "not valid" in said[0]


def test_the_plan_names_each_change_and_hides_nothing_surprising() -> None:
    live = read_live(FakeAws())
    new = apply_changes(live, parse(["--ai", "on", "--alert-email", "me@example.com"]))
    text = "\n".join(describe(live, new))
    assert "Live messaging: on\n" in text + "\n" and "Plaid: off\n" in text + "\n"
    assert "AI answers: on (was off)" in text
    assert "me@example.com (new)" in text
    assert "Second household member may use the bot: not set" in text
    assert "kept exactly as in the running stack" in text
    assert "ab" * 32 not in text  # IDs and keys are not echoed


def test_a_deploy_builds_diffs_then_deploys_with_the_live_values_in_the_environment() -> None:
    code, recorder, said = run(["--alert-email", "me@example.com"])
    assert code == 0
    assert recorder.verbs() == ["build", "diff", "deploy"]
    for _, env in recorder.commands:
        assert env["DISCORD_APPLICATION_ID"] == "100000000000000001"
        assert env["LIVE_MESSAGING_ENABLED"] == "true"  # the live bot is not paused by a redeploy
        assert env["ALERT_EMAIL"] == "me@example.com"
        assert env["PLAID_ENABLED"] == env["AI_ENABLED"] == "false"
        assert (
            env["CDK_DEFAULT_ACCOUNT"] == "123456789012"
            and env["CDK_DEFAULT_REGION"] == "us-east-2"
        )
        assert "DISCORD_MEMBER_2_ID" not in env
    deploy_command = recorder.commands[-1][0]
    assert deploy_command[-3:] == [deploy.STACK, "--require-approval", "never"]
    assert any("123456789012" in line for line in said)


def test_stale_shell_settings_cannot_leak_into_a_deploy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALERT_EMAIL", "stale@example.com")
    monkeypatch.setenv("DISCORD_MEMBER_2_ID", "999999999999999999")
    monkeypatch.setenv("PLAID_ENABLED", "true")
    _, recorder, _ = run([])
    for _, env in recorder.commands:
        assert "ALERT_EMAIL" not in env and "DISCORD_MEMBER_2_ID" not in env
        assert env["PLAID_ENABLED"] == "false"


def test_the_prompt_must_be_answered_yes_to_deploy() -> None:
    code, recorder, said = run([], answer="n")
    assert (
        code == 1 and recorder.verbs() == ["build", "diff"] and "Nothing was deployed" in said[-1]
    )
    code, recorder, _ = run([], answer="")
    assert code == 1 and "deploy" not in recorder.verbs()


def test_yes_skips_the_prompt_and_preview_never_deploys() -> None:
    def refuse(_prompt: str) -> str:
        raise AssertionError("should not ask")

    recorder = Recorder()
    assert main(["--yes"], FakeAws(), recorder, refuse, lambda _l: None) == 0
    assert recorder.verbs() == ["build", "diff", "deploy"]
    code, recorder, _ = run(["--preview"])
    assert code == 0 and recorder.verbs() == ["build", "diff"]


def test_a_second_member_is_carried_over(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {**LIVE_ENV, "DISCORD_MEMBER_2_ID": "100000000000000005"}
    _, recorder, _ = run([], FakeAws(env=env))
    assert all(e["DISCORD_MEMBER_2_ID"] == "100000000000000005" for _, e in recorder.commands)


SPOUSE = "100000000000000009"


def test_the_second_member_can_be_allowed_without_changing_anything_else() -> None:
    live = read_live(FakeAws())
    new = apply_changes(live, parse(["--member-2", f" {SPOUSE} "]))
    assert new["DISCORD_MEMBER_2_ID"] == SPOUSE
    assert {k: v for k, v in new.items() if k != "DISCORD_MEMBER_2_ID"} == live
    text = "\n".join(describe(live, new))
    assert "Second household member may use the bot: yes (new)" in text and SPOUSE not in text


def test_changing_an_existing_second_member_is_called_out() -> None:
    live = read_live(FakeAws(env={**LIVE_ENV, "DISCORD_MEMBER_2_ID": "100000000000000005"}))
    new = apply_changes(live, parse(["--member-2", SPOUSE]))
    assert "yes (changed)" in "\n".join(describe(live, new))
    assert "yes\n" in "\n".join(describe(live, live)) + "\n"


@pytest.mark.parametrize(
    "bad",
    [
        LIVE_ENV["DISCORD_MEMBER_1_ID"],
        LIVE_ENV["DISCORD_CHANNEL_ID"],
        LIVE_ENV["DISCORD_GUILD_ID"],
        LIVE_ENV["DISCORD_APPLICATION_ID"],
    ],
)
def test_an_id_that_belongs_to_something_else_is_refused(bad: str) -> None:
    code, recorder, said = run(["--member-2", bad])
    assert code == 2 and not recorder.commands and "already the app" in said[0]


@pytest.mark.parametrize("bad", ["", "alex", "12345", "<@100000000000000009>", "-1"])
def test_something_that_is_not_a_user_id_is_refused(bad: str) -> None:
    code, recorder, said = run(["--member-2", bad])
    assert code == 2 and not recorder.commands and "not valid" in said[0]


def test_the_second_member_reaches_cdk_in_the_environment() -> None:
    code, recorder, _ = run(["--member-2", SPOUSE])
    assert code == 0
    assert all(env["DISCORD_MEMBER_2_ID"] == SPOUSE for _, env in recorder.commands)


def test_a_missing_aws_cli_is_reported_as_a_deploy_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing() -> str:
        raise FileNotFoundError("The AWS CLI was not found. Install it, then run `aws login`.")

    monkeypatch.setattr(deploy, "aws_command", missing)
    with pytest.raises(DeployError, match="AWS CLI was not found"):
        deploy.run_aws(["sts", "get-caller-identity"])
