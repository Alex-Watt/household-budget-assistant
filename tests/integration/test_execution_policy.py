"""The scoped CloudFormation role must be able to create everything the stack defines."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.integration.test_stack import FULL, build

pytestmark = pytest.mark.integration

POLICY = Path(__file__).resolve().parents[2] / "infrastructure" / "cfn-execution-policy.json"
ACCOUNT, REGION = "123456789012", "us-east-2"


def policy() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(POLICY.read_text(encoding="utf-8"))
    return loaded


def statement_resources(sid: str) -> list[str]:
    (found,) = [s for s in policy()["Statement"] if s["Sid"] == sid]
    resource = found["Resource"]
    return [resource] if isinstance(resource, str) else resource


def test_policy_is_valid_scoped_and_has_no_administrator_access() -> None:
    document = policy()
    sids = [s["Sid"] for s in document["Statement"]]
    assert len(sids) == len(set(sids))
    text = json.dumps(document)
    assert "AdministratorAccess" not in text and '"Action": "*"' not in text
    for statement in document["Statement"]:
        assert statement["Effect"] == "Allow"
        assert account_scoped(statement), statement["Sid"]


def account_scoped(statement: dict[str, Any]) -> bool:
    resources = (
        statement["Resource"]
        if isinstance(statement["Resource"], list)
        else [statement["Resource"]]
    )
    wildcard_ok = {"logs:DescribeLogGroups", "cloudwatch:DescribeAlarms"}
    if resources == ["*"]:
        return statement["Action"] in wildcard_ok
    return all(ACCOUNT in r or r.startswith("arn:aws:s3:::cdk-") or "::/" in r for r in resources)


def test_policy_fits_the_iam_size_limit() -> None:
    # IAM rejects a managed policy over 6,144 characters, ignoring whitespace. This only shows up
    # at deploy time, halfway through publishing, so catch it here.
    compact = re.sub(r"\s", "", POLICY.read_text(encoding="utf-8"))
    assert len(compact) <= 6144, f"{len(compact)} characters; trim a statement"


def test_the_stack_only_creates_inline_role_policies_and_the_policy_does_not_allow_more(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CloudTrail showed CloudFormation calls PutRolePolicy for these, never CreatePolicy."""
    template = build(tmp_path, monkeypatch, **FULL).to_json()
    kinds = {r["Type"] for r in template["Resources"].values()}
    assert "AWS::IAM::Policy" in kinds and "AWS::IAM::ManagedPolicy" not in kinds
    actions = {
        action
        for statement in policy()["Statement"]
        for action in (
            statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        )
    }
    assert "iam:PutRolePolicy" in actions
    assert not {a for a in actions if "Policy" in a and a.endswith(("CreatePolicy", "Version"))}


def test_only_the_lambda_logging_policy_may_be_attached_to_stack_roles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attaching any managed policy (such as AdministratorAccess) is the easy escalation."""
    template = build(tmp_path, monkeypatch, **FULL).to_json()
    attached = {
        json.dumps(arn)
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::IAM::Role"
        for arn in resource["Properties"].get("ManagedPolicyArns", [])
    }
    assert attached and all("service-role/AWSLambdaBasicExecutionRole" in arn for arn in attached)
    (statement,) = [s for s in policy()["Statement"] if s["Sid"] == "AttachLambdaLoggingPolicy"]
    assert statement["Condition"] == {
        "ArnEquals": {
            "iam:PolicyARN": "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
        }
    }
    assert sorted(statement["Action"]) == ["iam:AttachRolePolicy", "iam:DetachRolePolicy"]
    # The unconditioned role statement must not also grant attach or detach.
    (roles,) = [s for s in policy()["Statement"] if s["Sid"] == "StackRoles"]
    assert not {"iam:AttachRolePolicy", "iam:DetachRolePolicy"} & set(roles["Action"])


def test_every_schedule_the_stack_creates_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = build(tmp_path, monkeypatch, **FULL).to_json()
    names = {
        resource["Properties"]["Name"]
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::Scheduler::Schedule"
    }
    allowed = set(statement_resources("Schedules"))
    assert names == {"HouseholdBudgetDailyReport", "HouseholdBudgetCardSync"}
    assert {
        f"arn:aws:scheduler:{REGION}:{ACCOUNT}:schedule/default/{name}" for name in names
    } == allowed


def test_every_resource_type_the_full_stack_needs_has_a_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    types = {
        resource["Type"]
        for resource in build(tmp_path, monkeypatch, **FULL).to_json()["Resources"].values()
    }
    needed = {
        "AWS::SNS::Topic": "AlertTopic",
        "AWS::CloudWatch::Alarm": "AlertAlarms",
        "AWS::Budgets::Budget": "MonthlyCostBudget",
        "AWS::Scheduler::Schedule": "Schedules",
        "AWS::DynamoDB::Table": "MessagingTable",
        "AWS::Lambda::EventInvokeConfig": "MessagingFunctions",
    }
    sids = {s["Sid"] for s in policy()["Statement"]}
    assert types >= set(needed)
    assert set(needed.values()) <= sids
