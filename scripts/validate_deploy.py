"""Fail before requesting AWS credentials if the deployment target is incomplete."""

import os
import re
from collections.abc import Mapping

from budget_bot.config import Settings


def validate(values: Mapping[str, str]) -> None:
    account = values.get("AWS_ACCOUNT_ID", "")
    region = values.get("AWS_REGION", "")
    role = values.get("AWS_DEPLOY_ROLE_ARN", "")
    if not re.fullmatch(r"[0-9]{12}", account):
        raise ValueError("Set AWS_ACCOUNT_ID to a 12-digit account ID")
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", region):
        raise ValueError("Set AWS_REGION to the target deployment region")
    expected = rf"arn:aws(?:-us-gov|-cn)?:iam::{account}:role/HouseholdBudgetGitHubDeployRole"
    if not re.fullmatch(expected, role):
        raise ValueError(
            "AWS_DEPLOY_ROLE_ARN must identify this account's HouseholdBudgetGitHubDeployRole"
        )
    settings = Settings.from_env(values)
    if settings.live_messaging:
        raise ValueError("The standard deployment workflow must keep live messaging disabled")
    if settings.plaid_enabled or settings.ai_enabled:
        raise ValueError("The standard deployment workflow must keep Plaid and AI disabled")


if __name__ == "__main__":
    validate(os.environ)
    print("Deployment configuration validated; live messaging, Plaid and AI remain disabled.")
