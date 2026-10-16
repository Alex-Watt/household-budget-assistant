import json
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from infrastructure.stack import HouseholdBudgetStack

pytestmark = pytest.mark.integration

FULL = {
    "AI_ENABLED": "true",
    "PLAID_ENABLED": "true",
    "ALERT_EMAIL": "household@example.com",
    "MONTHLY_COST_ALERT_USD": "50",
}


def build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str) -> Template:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    # CDK only needs an asset directory for assertions; production synth uses the real build.
    assets = tmp_path / ".build" / "lambda"
    assets.mkdir(parents=True)
    (assets / "placeholder.py").write_text("# test asset")
    app = cdk.App(outdir=str(tmp_path / "cdk.out"))
    return Template.from_stack(HouseholdBudgetStack(app, "Test", lambda_asset=assets))


@pytest.fixture
def template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Template:
    return build(tmp_path, monkeypatch)


@pytest.fixture
def full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Template:
    return build(tmp_path, monkeypatch, **FULL)


def function_roles(template: Template) -> dict[str, str]:
    functions = template.find_resources("AWS::Lambda::Function")
    return {name: props["Properties"]["Role"]["Fn::GetAtt"][0] for name, props in functions.items()}


def role_of(template: Template, prefix: str) -> str:
    return next(role for name, role in function_roles(template).items() if name.startswith(prefix))


def statements(template: Template) -> list[tuple[str, dict[str, Any]]]:
    """Every IAM statement as (role logical ID, statement)."""
    found = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        document = policy["Properties"]["PolicyDocument"]["Statement"]
        for role in policy["Properties"]["Roles"]:
            found.extend((role["Ref"], statement) for statement in document)
    return found


def roles_allowed(template: Template, action: str, resource: str = "") -> set[str]:
    """Roles granted `action` on a resource whose reference mentions `resource`."""
    roles = set()
    for role, statement in statements(template):
        actions = (
            statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        )
        if action in actions and resource in json.dumps(statement["Resource"]):
            roles.add(role)
    return roles


def test_stack_cannot_be_deleted_by_accident(tmp_path: Path) -> None:
    assets = tmp_path / ".build" / "lambda"
    assets.mkdir(parents=True)
    (assets / "placeholder.py").write_text("# test asset")
    stack = HouseholdBudgetStack(cdk.App(outdir=str(tmp_path / "out")), "Test", lambda_asset=assets)
    assert stack.termination_protection is True


def test_no_alert_outputs_when_no_email_is_set(template: Template) -> None:
    assert "AlertEmail" not in template.to_json().get("Outputs", {})


def test_alert_settings_are_published_as_outputs_so_a_redeploy_keeps_them(full: Template) -> None:
    outputs = full.to_json()["Outputs"]
    assert outputs["AlertEmail"]["Value"] == "household@example.com"
    assert outputs["MonthlyCostAlertUsd"]["Value"] == "50"


def test_default_stack_is_small_and_runs_on_current_arm_runtime(template: Template) -> None:
    template.resource_count_is("AWS::Lambda::Function", 3)
    for resource in template.find_resources("AWS::Lambda::Function").values():
        props = resource["Properties"]
        assert props["Runtime"] == "python3.14"
        assert props["Architectures"] == ["arm64"]
        variables = props["Environment"]["Variables"]
        assert variables["LIVE_MESSAGING_ENABLED"] == "false"
        assert variables["AI_ENABLED"] == "false" and variables["PLAID_ENABLED"] == "false"
    # Nothing optional is created until it is switched on.
    template.resource_count_is("AWS::Scheduler::Schedule", 1)
    template.resource_count_is("AWS::SNS::Topic", 0)
    template.resource_count_is("AWS::Budgets::Budget", 0)
    template.resource_count_is("AWS::CloudWatch::Alarm", 0)
    template.resource_count_is("AWS::SecretsManager::Secret", 0)
    assert not roles_allowed(template, "bedrock:InvokeModel")
    assert len(template.find_resources("AWS::ApiGatewayV2::Route")) == 1


def test_failure_controls(template: Template) -> None:
    # The worker may run a model for up to 90 seconds, so messages stay invisible for six times that.
    template.has_resource_properties(
        "AWS::SQS::Queue",
        {"VisibilityTimeout": 540, "RedrivePolicy": Match.object_like({"maxReceiveCount": 3})},
    )
    template.has_resource_properties(
        "AWS::Lambda::Function", {"Handler": "budget_bot.worker.lambda_handler", "Timeout": 90}
    )
    template.has_resource_properties(
        "AWS::Lambda::EventSourceMapping",
        {
            "BatchSize": 1,
            "FunctionResponseTypes": ["ReportBatchItemFailures"],
            "ScalingConfig": {"MaximumConcurrency": 2},
        },
    )
    assert (
        len(template.find_resources("AWS::Logs::LogGroup", {"Properties": {"RetentionInDays": 30}}))
        == 3
    )


def test_both_tables_are_pay_per_request_retained_and_recoverable(template: Template) -> None:
    template.resource_count_is("AWS::DynamoDB::Table", 2)
    for table in template.find_resources("AWS::DynamoDB::Table").values():
        assert table["DeletionPolicy"] == "Retain"
        props = table["Properties"]
        assert props["BillingMode"] == "PAY_PER_REQUEST"
        assert props["PointInTimeRecoverySpecification"] == {"PointInTimeRecoveryEnabled": True}
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "KeySchema": [
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            "GlobalSecondaryIndexes": [
                Match.object_like(
                    {
                        "IndexName": "by-month",
                        "KeySchema": [
                            {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                            {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                        ],
                        "Projection": {"ProjectionType": "ALL"},
                    }
                )
            ],
        },
    )


def test_daily_report_runs_at_8am_central(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Scheduler::Schedule",
        {
            "Name": "HouseholdBudgetDailyReport",
            "ScheduleExpression": "cron(0 8 * * ? *)",
            "ScheduleExpressionTimezone": "America/Chicago",
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "Target": Match.object_like(
                {"RetryPolicy": {"MaximumRetryAttempts": 2, "MaximumEventAgeInSeconds": 3600}}
            ),
        },
    )


def test_public_endpoint_is_throttled_and_queues_require_tls(template: Template) -> None:
    template.has_resource_properties("AWS::ApiGatewayV2::Route", {"RouteKey": "POST /discord"})
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Stage",
        {
            "StageName": "$default",
            "AutoDeploy": True,
            "DefaultRouteSettings": {"ThrottlingRateLimit": 5, "ThrottlingBurstLimit": 10},
            "RouteSettings": {
                "POST /discord": {"ThrottlingBurstLimit": 5, "ThrottlingRateLimit": 2}
            },
        },
    )
    template.resource_count_is("AWS::SQS::QueuePolicy", 2)
    for policy in template.find_resources("AWS::SQS::QueuePolicy").values():
        statement = policy["Properties"]["PolicyDocument"]["Statement"][0]
        assert statement["Effect"] == "Deny"
        assert statement["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}


def test_default_permissions_are_least_privilege(template: Template) -> None:
    webhook, worker, report = (role_of(template, p) for p in ("Webhook", "Worker", "Report"))
    assert roles_allowed(template, "secretsmanager:GetSecretValue") == {report}
    assert roles_allowed(template, "sqs:SendMessage") == {webhook}
    assert roles_allowed(template, "sqs:ReceiveMessage") == {worker}
    # With AI off nothing but the sync (disabled here) writes the ledger; the worker only reads.
    assert not roles_allowed(template, "dynamodb:PutItem", "Ledger")
    assert roles_allowed(template, "dynamodb:Query", "Ledger") == {worker, report}
    assert roles_allowed(template, "dynamodb:PutItem", "MessagingState") == {worker, report}
    assert not roles_allowed(template, "dynamodb:PutItem") & {webhook}


# Everything switched on


def test_full_stack_adds_plaid_ai_and_alerting(full: Template) -> None:
    full.resource_count_is("AWS::Lambda::Function", 5)
    for resource in full.find_resources("AWS::Lambda::Function").values():
        variables = resource["Properties"]["Environment"]["Variables"]
        assert variables["AI_ENABLED"] == "true" and variables["PLAID_ENABLED"] == "true"
        assert variables["AI_PROFILE_SCOPE"] == "us" and variables["AI_DAILY_LIMIT"] == "30"
    full.has_resource_properties("AWS::ApiGatewayV2::Route", {"RouteKey": "POST /plaid"})
    full.has_resource_properties("AWS::ApiGatewayV2::Route", {"RouteKey": "POST /discord"})
    full.has_resource_properties(
        "AWS::Scheduler::Schedule",
        {
            "Name": "HouseholdBudgetCardSync",
            "ScheduleExpression": "cron(30 7,19 * * ? *)",
            "ScheduleExpressionTimezone": "America/Chicago",
        },
    )
    full.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "budget_bot.sync.lambda_handler",
            "Timeout": 90,
        },
    )


def test_full_stack_keeps_each_credential_with_the_function_that_needs_it(full: Template) -> None:
    report, sync, hook, worker = (
        role_of(full, p) for p in ("Report", "Sync", "PlaidWebhook", "Worker")
    )
    assert roles_allowed(full, "secretsmanager:GetSecretValue", "discord") == {report}
    assert roles_allowed(full, "secretsmanager:GetSecretValue", "plaid") == {sync, hook}
    assert not roles_allowed(full, "secretsmanager:GetSecretValue", "plaid") & {report, worker}
    # Only the sync Lambda and the worker (corrections) write the ledger.
    assert roles_allowed(full, "dynamodb:PutItem", "Ledger") == {worker, sync}
    # Among the application functions, only the Plaid webhook may invoke anything: the sync.
    execution_roles = set(function_roles(full).values())
    assert roles_allowed(full, "lambda:InvokeFunction") & execution_roles == {hook}
    assert roles_allowed(full, "lambda:InvokeFunction", "Sync") & execution_roles == {hook}
    assert not roles_allowed(full, "dynamodb:Query") & {hook}


def test_the_workers_ledger_writes_are_limited_to_corrections_audit_replay_and_budget(
    full: Template,
) -> None:
    worker = role_of(full, "Worker")

    def ledger_statement(action: str) -> dict[str, Any]:
        (found,) = [
            s
            for role, s in statements(full)
            if role == worker
            and action in json.dumps(s["Action"])
            and "Ledger" in json.dumps(s["Resource"])
        ]
        return found

    write, delete = ledger_statement("dynamodb:PutItem"), ledger_statement("dynamodb:DeleteItem")
    assert write["Action"] == "dynamodb:PutItem" or list(write["Action"]) == ["dynamodb:PutItem"]
    assert write["Condition"] == {
        "ForAllValues:StringEquals": {
            "dynamodb:LeadingKeys": [
                "OVERRIDE",
                "AUDIT",
                "WRITE",
                "BUDGET",
                "ADJUST",
                "RULES",
                "DEFAULTS",
                "SPLIT",
            ]
        }
    }
    # Only an undone correction is ever deleted: the audit trail, replay guards and budget
    # records are never removed, and the worker is not allowed to remove them.
    assert delete["Condition"] == {
        "ForAllValues:StringEquals": {"dynamodb:LeadingKeys": ["OVERRIDE"]}
    }
    assert delete is not write
    # The worker cannot batch-write, update or scan the ledger, so it cannot touch transactions.
    allowed = {
        a
        for role, s in statements(full)
        if role == worker and "Ledger" in json.dumps(s["Resource"])
        for a in _actions(s)
    }
    assert not allowed & {"dynamodb:BatchWriteItem", "dynamodb:UpdateItem"}
    assert "dynamodb:Query" in allowed  # it can still read what it needs to answer questions


def _actions(statement: dict[str, Any]) -> list[str]:
    action = statement["Action"]
    return action if isinstance(action, list) else [action]


def test_only_the_worker_can_call_the_model_and_only_sonnet(full: Template) -> None:
    worker = role_of(full, "Worker")
    assert roles_allowed(full, "bedrock:InvokeModel") == {worker}
    assert roles_allowed(full, "bedrock:ListInferenceProfiles") == {worker}
    (statement,) = [
        s for role, s in statements(full) if role == worker and "bedrock:InvokeModel" in s["Action"]
    ]
    resources = json.dumps(statement["Resource"])
    assert "us.anthropic.claude-sonnet-*" in resources
    assert "foundation-model/anthropic.claude-sonnet-*" in resources
    assert "opus" not in resources and "haiku" not in resources
    assert statement["Action"] in ("bedrock:InvokeModel", ["bedrock:InvokeModel"])


def test_alarms_and_cost_budget_go_to_the_alert_email(full: Template) -> None:
    full.resource_count_is("AWS::SNS::Topic", 1)
    full.has_resource_properties(
        "AWS::SNS::Subscription", {"Protocol": "email", "Endpoint": "household@example.com"}
    )
    # One error alarm per function, the dead-letter queue, the missing morning report,
    # a public-traffic alarm and a model-token alarm.
    full.resource_count_is("AWS::CloudWatch::Alarm", 9)
    full.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {"AlarmName": Match.absent(), "MetricName": "Errors", "Threshold": 1},
    )
    full.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "MetricName": "Invocations",
            "ComparisonOperator": "LessThanThreshold",
            "TreatMissingData": "breaching",
            "Period": 86400,
        },
    )
    full.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {"MetricName": "ApproximateNumberOfMessagesVisible", "Threshold": 1},
    )
    full.has_resource_properties(
        "AWS::Budgets::Budget",
        {
            "Budget": Match.object_like(
                {
                    "BudgetType": "COST",
                    "TimeUnit": "MONTHLY",
                    "BudgetLimit": {"Amount": 50, "Unit": "USD"},
                }
            ),
            "NotificationsWithSubscribers": [
                Match.object_like(
                    {
                        "Notification": Match.object_like(
                            {"NotificationType": "ACTUAL", "Threshold": 80}
                        ),
                        "Subscribers": [
                            {"SubscriptionType": "EMAIL", "Address": "household@example.com"}
                        ],
                    }
                ),
                Match.object_like(
                    {
                        "Notification": Match.object_like(
                            {"NotificationType": "FORECASTED", "Threshold": 100}
                        )
                    }
                ),
            ],
        },
    )


def test_global_model_scope_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    template = build(tmp_path, monkeypatch, AI_ENABLED="true", AI_PROFILE_SCOPE="global")
    assert "global.anthropic.claude-sonnet-*" in json.dumps(template.to_json())
