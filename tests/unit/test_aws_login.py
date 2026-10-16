import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import MissingDependencyException

from budget_bot import aws_login
from budget_bot.aws_login import aws_command, use_login_credentials

SESSION = {
    "Version": 1,
    "AccessKeyId": "ASIAEXAMPLEKEY",
    "SecretAccessKey": "example-secret",
    "SessionToken": "example-token",
    "Expiration": "2026-10-14T00:00:00+00:00",
}


class Runner:
    def __init__(self, stdout: str = "", code: int = 0) -> None:
        self.result = SimpleNamespace(stdout=stdout, returncode=code, stderr="")
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **_kwargs: Any) -> Any:
        self.calls.append(command)
        return self.result


@pytest.fixture
def no_default_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """boto3 as it behaves with an `aws login` session and no awscrt installed."""

    class Session:
        def get_credentials(self) -> None:
            raise MissingDependencyException(msg="login credentials need awscrt")

    monkeypatch.setattr(aws_login.boto3, "Session", Session)
    monkeypatch.setattr(aws_login, "aws_command", lambda: "aws")


def test_existing_environment_credentials_are_left_alone() -> None:
    env = {"AWS_ACCESS_KEY_ID": "a", "AWS_SECRET_ACCESS_KEY": "b"}
    runner = Runner()
    use_login_credentials(env, runner)
    assert not runner.calls and env == {"AWS_ACCESS_KEY_ID": "a", "AWS_SECRET_ACCESS_KEY": "b"}


def test_credentials_boto3_can_already_read_are_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    class Session:
        def get_credentials(self) -> object:
            return object()

    monkeypatch.setattr(aws_login.boto3, "Session", Session)
    runner = Runner()
    env: dict[str, str] = {}
    use_login_credentials(env, runner)
    assert not runner.calls and not env


def test_a_login_session_is_borrowed_from_the_cli_for_this_process_only(
    no_default_credentials: None,
) -> None:
    runner, env = Runner(json.dumps(SESSION)), {}
    use_login_credentials(env, runner)
    assert runner.calls == [["aws", "configure", "export-credentials", "--format", "process"]]
    assert env == {
        "AWS_ACCESS_KEY_ID": "ASIAEXAMPLEKEY",
        "AWS_SECRET_ACCESS_KEY": "example-secret",
        "AWS_SESSION_TOKEN": "example-token",
        "AWS_DEFAULT_REGION": "us-east-2",
    }


def test_an_existing_region_is_kept_and_a_missing_token_is_not_invented(
    no_default_credentials: None,
) -> None:
    session = {k: v for k, v in SESSION.items() if k != "SessionToken"}
    env = {"AWS_DEFAULT_REGION": "us-west-2"}
    use_login_credentials(env, Runner(json.dumps(session)))
    assert env["AWS_DEFAULT_REGION"] == "us-west-2" and "AWS_SESSION_TOKEN" not in env


def test_not_signed_in_gives_a_clear_message_without_leaking_cli_output(
    no_default_credentials: None,
) -> None:
    with pytest.raises(RuntimeError, match="aws login"):
        use_login_credentials({}, Runner("secret-looking text", code=1))


@pytest.mark.parametrize("output", ["not json", "{}", '{"AccessKeyId": "only-half"}'])
def test_unexpected_cli_output_is_refused(no_default_credentials: None, output: str) -> None:
    env: dict[str, str] = {}
    with pytest.raises(RuntimeError, match="unexpected form") as caught:
        use_login_credentials(env, Runner(output))
    assert "only-half" not in str(caught.value) and not env.get("AWS_SECRET_ACCESS_KEY")


def test_scripts_stop_with_a_plain_message_when_not_signed_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse() -> None:
        raise RuntimeError("Could not read AWS credentials. Run `aws login` and try again.")

    monkeypatch.setattr(aws_login, "use_login_credentials", refuse)
    with pytest.raises(SystemExit, match="aws login"):
        aws_login.sign_in_or_exit()


def test_the_aws_cli_is_found_on_the_path_or_in_the_standard_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(aws_login.shutil, "which", lambda _name: r"C:\tools\aws.exe")
    assert aws_command() == r"C:\tools\aws.exe"
    installed = tmp_path / "aws.exe"
    installed.write_text("")
    monkeypatch.setattr(aws_login.shutil, "which", lambda _name: None)
    monkeypatch.setattr(aws_login, "AWS_FALLBACK", installed)
    assert aws_command() == str(installed)
    monkeypatch.setattr(aws_login, "AWS_FALLBACK", tmp_path / "missing.exe")
    with pytest.raises(FileNotFoundError, match="AWS CLI was not found"):
        aws_command()
