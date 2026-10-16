"""Lets the local helper scripts use an `aws login` session. Not used by the Lambdas.

boto3 cannot read the credentials `aws login` creates unless an extra compiled package
(`awscrt`) is installed, but the AWS CLI can hand them over. This borrows the session's
temporary credentials for the current process only: they go into this process's environment
variables, never to disk, and expire on their own.
"""

import json
import os
import shutil
import subprocess
from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError

AWS_FALLBACK = Path(r"C:\Program Files\Amazon\AWSCLIV2\aws.exe")


def aws_command() -> str:
    """The AWS CLI, from PATH or its standard Windows install folder."""
    found = shutil.which("aws")
    if found:
        return found
    if AWS_FALLBACK.exists():
        return str(AWS_FALLBACK)
    raise FileNotFoundError("The AWS CLI was not found. Install it, then run `aws login`.")


def use_login_credentials(
    environ: MutableMapping[str, str] | None = None,
    run: Callable[..., Any] = subprocess.run,
) -> None:
    """Make boto3 usable in this process, borrowing the AWS CLI's session if it must."""
    env = os.environ if environ is None else environ
    if env.get("AWS_ACCESS_KEY_ID") and env.get("AWS_SECRET_ACCESS_KEY"):
        return
    try:
        if boto3.Session().get_credentials() is not None:
            return
    except BotoCoreError:
        pass  # typically the login provider asking for awscrt; the CLI can do it instead
    done = run(
        [aws_command(), "configure", "export-credentials", "--format", "process"],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise RuntimeError("Could not read AWS credentials. Run `aws login` and try again.")
    try:
        data = json.loads(done.stdout)
        env["AWS_ACCESS_KEY_ID"] = data["AccessKeyId"]
        env["AWS_SECRET_ACCESS_KEY"] = data["SecretAccessKey"]
    except (ValueError, KeyError) as exc:
        raise RuntimeError("The AWS CLI returned credentials in an unexpected form.") from exc
    if data.get("SessionToken"):
        env["AWS_SESSION_TOKEN"] = data["SessionToken"]
    env.setdefault("AWS_DEFAULT_REGION", "us-east-2")


def sign_in_or_exit() -> None:
    """For scripts: `use_login_credentials`, but a problem ends the script with a plain message."""
    try:
        use_login_credentials()
    except (RuntimeError, FileNotFoundError) as exc:
        raise SystemExit(str(exc)) from exc
