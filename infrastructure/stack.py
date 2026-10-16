from pathlib import Path

from aws_cdk import (
    ArnFormat,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    TimeZone,
    aws_apigatewayv2 as apigw,
    aws_apigatewayv2_integrations as integrations,
    aws_budgets as budgets,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_lambda_event_sources as sources,
    aws_logs as logs,
    aws_scheduler as scheduler,
    aws_scheduler_targets as targets,
    aws_secretsmanager as secretsmanager,
    aws_sns as sns,
    aws_sns_subscriptions as subscriptions,
    aws_sqs as sqs,
)
from constructs import Construct

from budget_bot.config import Settings

WORKER_TIMEOUT_SECONDS = 90


class HouseholdBudgetStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        lambda_asset: Path | None = None,
        **kwargs: object,
    ) -> None:
        # The stack holds the household's transaction history. Deleting it must be a deliberate
        # two-step (turn this off, then delete), not a slip. The tables are also retained.
        kwargs.setdefault("termination_protection", True)
        super().__init__(scope, construct_id, **kwargs)
        settings = Settings.from_env()
        secret = secretsmanager.Secret.from_secret_name_v2(
            self, "DiscordSecret", settings.secret_name
        )

        table = dynamodb.Table(
            self,
            "MessagingState",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.RETAIN,
            deletion_protection=True,
        )
        # Transactions, corrections and sync bookkeeping. See budget_bot/ledger.py for the keys.
        ledger = dynamodb.Table(
            self,
            "Ledger",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.RETAIN,
            deletion_protection=True,
        )
        ledger.add_global_secondary_index(
            index_name="by-month",
            partition_key=dynamodb.Attribute(name="gsi1pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="gsi1sk", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        dlq = sqs.Queue(
            self, "InboundDeadLetter", retention_period=Duration.days(14), enforce_ssl=True
        )
        queue = sqs.Queue(
            self,
            "InboundQueue",
            # AWS recommends at least six times the function timeout.
            visibility_timeout=Duration.seconds(WORKER_TIMEOUT_SECONDS * 6),
            dead_letter_queue=sqs.DeadLetterQueue(queue=dlq, max_receive_count=3),
            retention_period=Duration.days(4),
            enforce_ssl=True,
        )
        common = {
            "DISCORD_APPLICATION_ID": settings.application_id,
            "DISCORD_PUBLIC_KEY": settings.public_key,
            "DISCORD_GUILD_ID": settings.guild_id,
            "DISCORD_CHANNEL_ID": settings.channel_id,
            "DISCORD_MEMBER_1_ID": settings.member_ids[0],
            **(
                {"DISCORD_MEMBER_2_ID": settings.member_ids[1]}
                if len(settings.member_ids) == 2
                else {}
            ),
            "DISCORD_SECRET_NAME": settings.secret_name,
            "STATE_TABLE": table.table_name,
            "LEDGER_TABLE": ledger.table_name,
            "LIVE_MESSAGING_ENABLED": str(settings.live_messaging).lower(),
            "AI_ENABLED": str(settings.ai_enabled).lower(),
            "AI_DAILY_LIMIT": str(settings.ai_daily_limit),
            "AI_PROFILE_SCOPE": settings.ai_profile_scope,
            "PLAID_ENABLED": str(settings.plaid_enabled).lower(),
            "PLAID_SECRET_NAME": settings.plaid_secret_name,
        }
        asset_path = lambda_asset or Path(__file__).resolve().parents[1] / ".build" / "lambda"
        if not asset_path.is_dir():
            raise ValueError("Build the Lambda asset first: python scripts/build_lambda.py")
        code = lambda_.Code.from_asset(str(asset_path))

        def function(
            name: str, handler: str, timeout: int, **extra: object
        ) -> tuple[lambda_.Function, logs.LogGroup]:
            log_group = logs.LogGroup(self, f"{name}Logs", retention=logs.RetentionDays.ONE_MONTH)
            fn = lambda_.Function(
                self,
                name,
                runtime=lambda_.Runtime.PYTHON_3_14,
                architecture=lambda_.Architecture.ARM_64,
                handler=f"budget_bot.{handler}.lambda_handler",
                code=code,
                timeout=Duration.seconds(timeout),
                memory_size=256,
                log_group=log_group,
                **extra,
            )
            return fn, log_group

        webhook, _ = function(
            "Webhook", "webhook", 5, environment={**common, "QUEUE_URL": queue.queue_url}
        )
        worker, _ = function("Worker", "worker", WORKER_TIMEOUT_SECONDS, environment=common)
        report, _ = function("Report", "report", 30, environment=common)
        functions = [webhook, worker, report]

        queue.grant_send_messages(webhook)
        queue.grant_consume_messages(worker)
        table.grant_read_write_data(worker)
        table.grant_read_write_data(report)
        ledger.grant_read_data(worker)
        ledger.grant_read_data(report)
        secret.grant_read(report)
        worker.add_event_source(
            sources.SqsEventSource(
                queue, batch_size=1, report_batch_item_failures=True, max_concurrency=2
            )
        )
        if settings.ai_enabled:
            # What the model may change: category corrections, budget changes, charge
            # adjustments, merchant rules and standing limits. Limit its writes to those
            # partitions (plus the audit and replay-guard ones), so a bug or an injection
            # cannot touch a transaction, a cursor or a lock.
            worker.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["dynamodb:PutItem"],
                    resources=[ledger.table_arn],
                    conditions={
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
                    },
                )
            )
            # Only an undone correction is ever deleted. Audit entries, replay guards and budget
            # records are never removed, so the worker is not allowed to.
            worker.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["dynamodb:DeleteItem"],
                    resources=[ledger.table_arn],
                    conditions={
                        "ForAllValues:StringEquals": {"dynamodb:LeadingKeys": ["OVERRIDE"]}
                    },
                )
            )
            # Only the worker may call the model, and only Claude Sonnet. The profile ARN is in
            # this Region; a US profile may route to other US Regions, hence the wildcard model.
            worker.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["bedrock:InvokeModel"],
                    resources=[
                        self.format_arn(
                            service="bedrock",
                            resource="inference-profile",
                            resource_name=f"{settings.ai_profile_scope}.anthropic.claude-sonnet-*",
                            arn_format=ArnFormat.SLASH_RESOURCE_NAME,
                        ),
                        "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-*",
                    ],
                )
            )
            worker.add_to_role_policy(
                iam.PolicyStatement(actions=["bedrock:ListInferenceProfiles"], resources=["*"])
            )

        scheduler.Schedule(
            self,
            "DailyReport",
            schedule_name="HouseholdBudgetDailyReport",
            description="Household budget update in Discord at 8 AM Central",
            schedule=scheduler.ScheduleExpression.cron(
                minute="0", hour="8", time_zone=TimeZone.AMERICA_CHICAGO
            ),
            target=targets.LambdaInvoke(report, retry_attempts=2, max_event_age=Duration.hours(1)),
        )
        api = apigw.HttpApi(self, "WebhookApi", create_default_stage=False)
        stage = api.add_stage(
            "DefaultStage",
            stage_name="$default",
            auto_deploy=True,
            # A ceiling for the stage. Each route below is tighter, because a flood of either
            # address used to share one bucket with real Discord traffic.
            throttle=apigw.ThrottleSettings(rate_limit=5, burst_limit=10),
        )
        api.add_routes(
            path="/discord",
            methods=[apigw.HttpMethod.POST],
            integration=integrations.HttpLambdaIntegration("WebhookIntegration", webhook),
        )
        route_limits = {
            "POST /discord": apigw.CfnStage.RouteSettingsProperty(
                throttling_rate_limit=2, throttling_burst_limit=5
            )
        }

        if settings.plaid_enabled:
            plaid_secret = secretsmanager.Secret.from_secret_name_v2(
                self, "PlaidSecret", settings.plaid_secret_name
            )
            sync, _ = function("Sync", "sync", 90, environment=common, retry_attempts=1)
            plaid_hook, _ = function(
                "PlaidWebhook",
                "plaid_webhook",
                10,
                environment={**common, "SYNC_FUNCTION": sync.function_name},
            )
            functions += [sync, plaid_hook]
            # Sync writes transactions, cursors, freshness and its own lock. It cannot delete
            # the audit trail, a correction, a budget or a household setting.
            sync.add_to_role_policy(
                iam.PolicyStatement(
                    actions=[
                        "dynamodb:GetItem",
                        "dynamodb:Query",
                        "dynamodb:PutItem",
                        "dynamodb:UpdateItem",
                        "dynamodb:BatchWriteItem",
                        "dynamodb:DeleteItem",
                    ],
                    resources=[ledger.table_arn, ledger.table_arn + "/index/*"],
                    conditions={
                        "ForAllValues:StringLike": {
                            "dynamodb:LeadingKeys": ["TXN#*", "CURSOR", "STATUS", "LOCK"]
                        }
                    },
                )
            )
            plaid_secret.grant_read(sync)
            plaid_secret.grant_read(plaid_hook)
            sync.grant_invoke(plaid_hook)
            # Plaid refreshes cards one to four times a day; webhooks cover new data, and these
            # two runs make sure the 8 AM report is fresh even if a webhook is missed.
            scheduler.Schedule(
                self,
                "CardSync",
                schedule_name="HouseholdBudgetCardSync",
                description="Pull new card transactions at 7:30 AM and 7:30 PM Central",
                schedule=scheduler.ScheduleExpression.cron(
                    minute="30", hour="7,19", time_zone=TimeZone.AMERICA_CHICAGO
                ),
                target=targets.LambdaInvoke(
                    sync, retry_attempts=2, max_event_age=Duration.hours(1)
                ),
            )
            api.add_routes(
                path="/plaid",
                methods=[apigw.HttpMethod.POST],
                integration=integrations.HttpLambdaIntegration(
                    "PlaidWebhookIntegration", plaid_hook
                ),
            )
            route_limits["POST /plaid"] = apigw.CfnStage.RouteSettingsProperty(
                throttling_rate_limit=1, throttling_burst_limit=5
            )
            CfnOutput(self, "PlaidWebhookUrl", value=f"{api.api_endpoint}/plaid")
            CfnOutput(self, "SyncFunctionName", value=sync.function_name)

        # The CDK property serializes these names in camelCase, which API Gateway rejects.
        stage.node.default_child.add_property_override(
            "RouteSettings",
            {
                key: {
                    "ThrottlingRateLimit": value.throttling_rate_limit,
                    "ThrottlingBurstLimit": value.throttling_burst_limit,
                }
                for key, value in route_limits.items()
            },
        )
        if settings.alert_email:
            self._alerts(settings, functions, report, dlq, api)
            # Not secrets, and scripts/deploy.py reads them back so a redeploy keeps them.
            CfnOutput(self, "AlertEmail", value=settings.alert_email)
            CfnOutput(self, "MonthlyCostAlertUsd", value=str(settings.monthly_cost_alert_usd))

        CfnOutput(self, "InteractionsUrl", value=f"{api.api_endpoint}/discord")
        CfnOutput(self, "LiveMessaging", value=str(settings.live_messaging).lower())
        CfnOutput(self, "AiEnabled", value=str(settings.ai_enabled).lower())
        CfnOutput(self, "PlaidEnabled", value=str(settings.plaid_enabled).lower())
        CfnOutput(self, "LedgerTableName", value=ledger.table_name)

    def _alerts(
        self,
        settings: Settings,
        functions: list[lambda_.Function],
        report: lambda_.Function,
        dlq: sqs.Queue,
        api: apigw.HttpApi,
    ) -> None:
        """Email on failures, a missed morning report, stuck messages and rising cost."""
        topic = sns.Topic(self, "Alerts")
        topic.add_subscription(subscriptions.EmailSubscription(settings.alert_email))
        action = cw_actions.SnsAction(topic)
        for fn in functions:
            alarm = fn.metric_errors(period=Duration.minutes(5)).create_alarm(
                self,
                f"{fn.node.id}Errors",
                threshold=1,
                evaluation_periods=1,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description="The function failed. Check its CloudWatch log group.",
            )
            alarm.add_alarm_action(action)
        stuck = dlq.metric_approximate_number_of_messages_visible(
            period=Duration.minutes(5)
        ).create_alarm(
            self,
            "DeadLetterNotEmpty",
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description="A question could not be processed. Inspect the dead-letter queue.",
        )
        stuck.add_alarm_action(action)
        silent = report.metric_invocations(period=Duration.hours(24), statistic="Sum").create_alarm(
            self,
            "ReportDidNotRun",
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
            alarm_description="The 8 AM report did not run in the last day.",
        )
        silent.add_alarm_action(action)
        flood = cloudwatch.Alarm(
            self,
            "ApiFlood",
            metric=cloudwatch.Metric(
                namespace="AWS/ApiGateway",
                metric_name="Count",
                dimensions_map={"ApiId": api.api_id, "Stage": "$default"},
                statistic="Sum",
                period=Duration.minutes(5),
            ),
            threshold=1000,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description="A public endpoint is being called far above normal household use.",
        )
        flood.add_alarm_action(action)
        tokens = cloudwatch.Alarm(
            self,
            "BedrockTokens",
            metric=cloudwatch.Metric(
                namespace="AWS/Bedrock",
                metric_name="InputTokenCount",
                statistic="Sum",
                period=Duration.hours(1),
            ),
            threshold=100_000,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description="Model input tokens in the last hour are far above normal use.",
        )
        tokens.add_alarm_action(action)
        subscriber = budgets.CfnBudget.SubscriberProperty(
            subscription_type="EMAIL", address=settings.alert_email
        )
        budgets.CfnBudget(
            self,
            "MonthlyCost",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(
                    amount=settings.monthly_cost_alert_usd, unit="USD"
                ),
            ),
            notifications_with_subscribers=[
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        notification_type=kind,
                        comparison_operator="GREATER_THAN",
                        threshold=threshold,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[subscriber],
                )
                for kind, threshold in (("ACTUAL", 80), ("FORECASTED", 100))
            ],
        )
