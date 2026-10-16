"""Deploy the stack from this PC, keeping every setting that is live now. Needs `aws login`.

    python scripts/deploy.py                              # show the plan and the diff, then ask
    python scripts/deploy.py --alert-email you@example.com
    python scripts/deploy.py --member-2 <their Discord user id>   # allow the second spouse
    python scripts/deploy.py --plaid on --ai on --yes     # no prompt
    python scripts/deploy.py --preview                    # stop after the diff

It reads the running stack (Discord IDs, live messaging, Plaid, AI and alert settings) and passes
them to CDK unchanged, so a deploy can never alter a setting just by forgetting it. A flag changes
only the setting it names. If the running stack cannot be read it stops: it never falls back to
defaults, because the default for live messaging is off and would silently pause the bot.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from budget_bot.aws_login import aws_command
from budget_bot.config import Settings

ROOT = Path(__file__).resolve().parents[1]
STACK = "HouseholdBudgetMessaging"
REGION = "us-east-2"

# Environment of the running worker that must be carried over to the next deploy.
CARRIED = (
    "DISCORD_APPLICATION_ID",
    "DISCORD_PUBLIC_KEY",
    "DISCORD_GUILD_ID",
    "DISCORD_CHANNEL_ID",
    "DISCORD_MEMBER_1_ID",
    "DISCORD_MEMBER_2_ID",
    "DISCORD_SECRET_NAME",
    "LIVE_MESSAGING_ENABLED",
    "AI_ENABLED",
    "AI_DAILY_LIMIT",
    "AI_PROFILE_SCOPE",
    "PLAID_ENABLED",
    "PLAID_SECRET_NAME",
)
REQUIRED = (
    "DISCORD_APPLICATION_ID",
    "DISCORD_PUBLIC_KEY",
    "DISCORD_GUILD_ID",
    "DISCORD_CHANNEL_ID",
    "DISCORD_MEMBER_1_ID",
    "LIVE_MESSAGING_ENABLED",
)
# Everything CDK reads from the environment, cleared first so a stale shell value cannot leak in.
MANAGED = (*CARRIED, "ALERT_EMAIL", "MONTHLY_COST_ALERT_USD")
FEATURES = (
    ("LIVE_MESSAGING_ENABLED", "Live messaging"),
    ("PLAID_ENABLED", "Plaid"),
    ("AI_ENABLED", "AI answers"),
)

AwsCall = Callable[[Sequence[str]], Any]
Execute = Callable[[Sequence[str], Mapping[str, str]], None]


class DeployError(RuntimeError):
    pass


def run_aws(args: Sequence[str]) -> Any:
    """Run one AWS CLI call and return its JSON output."""
    try:
        cli = aws_command()
    except FileNotFoundError as exc:
        raise DeployError(str(exc)) from exc
    done = subprocess.run(
        [cli, *args, "--region", REGION, "--output", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        detail = done.stderr.strip().splitlines()[-1:] or ["no details"]
        raise DeployError(f"AWS call failed ({args[0]} {args[1]}): {detail[0][:200]}")
    return json.loads(done.stdout) if done.stdout.strip() else {}


def run_command(command: Sequence[str], env: Mapping[str, str]) -> None:
    subprocess.run(list(command), cwd=ROOT, env=dict(env), check=True)


def read_live(aws: AwsCall) -> dict[str, str]:
    """The settings the running stack uses now."""
    try:
        stack = aws(["cloudformation", "describe-stacks", "--stack-name", STACK])["Stacks"][0]
        resources = aws(["cloudformation", "list-stack-resources", "--stack-name", STACK])[
            "StackResourceSummaries"
        ]
    except (KeyError, IndexError) as exc:
        raise DeployError(f"Could not read the {STACK} stack. Is it deployed?") from exc
    worker = next(
        (
            r["PhysicalResourceId"]
            for r in resources
            if r["ResourceType"] == "AWS::Lambda::Function"
            and r["LogicalResourceId"].startswith("Worker")
        ),
        None,
    )
    if worker is None:
        raise DeployError("The running stack has no worker function to read settings from.")
    config = aws(["lambda", "get-function-configuration", "--function-name", worker])
    env = (config.get("Environment") or {}).get("Variables") or {}
    missing = [key for key in REQUIRED if key not in env]
    if missing:
        raise DeployError(f"The running stack is missing {', '.join(missing)}; not deploying.")
    live = {key: env[key] for key in CARRIED if key in env}
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
    if "AlertEmail" in outputs:
        live["ALERT_EMAIL"] = outputs["AlertEmail"]
        live["MONTHLY_COST_ALERT_USD"] = outputs.get("MonthlyCostAlertUsd", "50")
    return live


def apply_changes(live: Mapping[str, str], args: argparse.Namespace) -> dict[str, str]:
    """The live settings with only the requested changes applied."""
    settings = {"PLAID_ENABLED": "false", "AI_ENABLED": "false", **live}
    for flag, key in (
        ("live", "LIVE_MESSAGING_ENABLED"),
        ("plaid", "PLAID_ENABLED"),
        ("ai", "AI_ENABLED"),
    ):
        value = getattr(args, flag)
        if value is not None:
            settings[key] = "true" if value == "on" else "false"
    if args.alert_email is not None:
        if args.alert_email.lower() == "none":
            settings.pop("ALERT_EMAIL", None)
        else:
            settings["ALERT_EMAIL"] = args.alert_email
    if args.monthly_budget is not None:
        settings["MONTHLY_COST_ALERT_USD"] = str(args.monthly_budget)
    if args.member_2 is not None:
        member = args.member_2.strip()
        if not member:
            raise DeployError(
                "Those settings are not valid: --member-2 needs the second person's user ID."
            )
        taken = (
            "DISCORD_APPLICATION_ID",
            "DISCORD_GUILD_ID",
            "DISCORD_CHANNEL_ID",
            "DISCORD_MEMBER_1_ID",
        )
        if member in (settings.get(key) for key in taken):
            raise DeployError(
                "That ID is already the app, server, channel or first member. "
                "Use the second person's own Discord user ID."
            )
        settings["DISCORD_MEMBER_2_ID"] = member
    try:
        Settings.from_env(settings)
    except ValueError as exc:
        raise DeployError(f"Those settings are not valid: {exc}") from exc
    return settings


def describe(live: Mapping[str, str], new: Mapping[str, str]) -> list[str]:
    lines = []
    for key, label in FEATURES:
        before, after = live.get(key, "false"), new.get(key, "false")
        state = "on" if after == "true" else "off"
        lines.append(
            f"  {label}: {state}"
            + (f" (was {'on' if before == 'true' else 'off'})" if before != after else "")
        )
    before_email, after_email = live.get("ALERT_EMAIL"), new.get("ALERT_EMAIL")
    if after_email:
        note = "" if before_email == after_email else " (new)" if not before_email else " (changed)"
        lines.append(f"  Alerts and the cost budget go to: {after_email}{note}")
    else:
        lines.append("  Alerts: off" + (" (was on; they will be removed)" if before_email else ""))
    before_member, after_member = live.get("DISCORD_MEMBER_2_ID"), new.get("DISCORD_MEMBER_2_ID")
    if after_member:
        note = (
            "" if before_member == after_member else " (new)" if not before_member else " (changed)"
        )
        lines.append(f"  Second household member may use the bot: yes{note}")
    else:
        lines.append("  Second household member may use the bot: not set")
    lines.append("  Other Discord IDs and secret names: kept exactly as in the running stack")
    return lines


def parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    for name, text in (
        ("live", "turn Discord replies and the 8 AM post on or off"),
        ("plaid", "turn Plaid card sync on or off"),
        ("ai", "turn Claude Sonnet answers on or off"),
    ):
        parser.add_argument(f"--{name}", choices=["on", "off"], help=text)
    parser.add_argument("--alert-email", help="email for alarms and the cost budget, or 'none'")
    parser.add_argument("--monthly-budget", type=int, help="USD for the cost budget (default 50)")
    parser.add_argument(
        "--member-2",
        metavar="USER_ID",
        help="allow the second household member (their Discord user ID) to use the bot",
    )
    parser.add_argument("--preview", action="store_true", help="stop after showing the diff")
    parser.add_argument("--yes", action="store_true", help="deploy without asking")
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    aws: AwsCall = run_aws,
    execute: Execute = run_command,
    ask: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
) -> int:
    args = parse(argv)
    try:
        live = read_live(aws)
        settings = apply_changes(live, args)
        account = aws(["sts", "get-caller-identity"])["Account"]
    except DeployError as exc:
        out(f"Stopped: {exc}")
        return 2
    out(f"Deploying {STACK} to account {account} in {REGION}. Settings:")
    for line in describe(live, settings):
        out(line)
    env = {key: value for key, value in os.environ.items() if key not in MANAGED}
    env.update(settings)
    env.update(CDK_DEFAULT_ACCOUNT=str(account), CDK_DEFAULT_REGION=REGION)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    npx = shutil.which("npx") or "npx"
    cdk = [npx, "--no-install", "cdk"]
    execute([sys.executable, "scripts/build_lambda.py"], env)
    execute([*cdk, "diff", STACK], env)
    if args.preview:
        return 0
    if not args.yes and ask("Deploy these changes? [y/N]: ").strip().lower() != "y":
        out("Nothing was deployed.")
        return 1
    execute([*cdk, "deploy", STACK, "--require-approval", "never"], env)
    return 0


if __name__ == "__main__":
    sys.exit(main())
