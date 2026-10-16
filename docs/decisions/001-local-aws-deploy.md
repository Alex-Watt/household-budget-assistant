# Deploy from this PC and keep the project spend limit

[Back to README](../../README.md) · [Deployment guide](../deployment.md) · [Roadmap](../roadmap.md)

**Status:** Accepted; messaging provider since replaced by [decision 002](002-discord-channel.md)  
**Date:** 13 October 2026

## Context

Milestone 1 is implemented locally. Telnyx has not approved the household group-MMS case, so the live three-phone proof is blocked. The next infrastructure step is to put the existing messaging stack in AWS with outbound messaging still disabled.

The examples in this repository use account `123456789012` and region `us-east-2`. Sign in with `aws login`. That session uses short-lived credentials. No access keys are created.

This project is AWS’s newer preconfigured account, not a full account. Its organization policy explicitly denies `iam:CreateOpenIDConnectProvider`. GitHub Actions therefore cannot be given a deploy role in this project. In `us-east-2`, CloudFormation, S3, Lambda, DynamoDB, SQS, API Gateway, Secrets Manager, and ordinary IAM roles are allowed. `us-east-1` is outside the project’s region allowance.

Turning on advanced features would hand over the organization policies and is the way to unblock GitHub deploys later. That change cannot be undone. It also removes the project spend limit: the automatic ceiling that can refuse new Lambda functions, databases, and queues and can pause the project. Budget alerts can replace the warning, but they do not freeze the account. The $100 credit is separate from that ceiling.

## Decision

Keep the project spend limit. Do not upgrade the account or activate advanced features for this deploy.

Deploy the messaging stack from this PC into account `123456789012` in `us-east-2`. The app still runs in AWS. This computer only runs the deploy command, using `aws login` short-lived credentials.

Leave GitHub Actions deployment off. Pull-request checks stay as they are. Do not create the GitHub OIDC provider, do not create `HouseholdBudgetGitHubDeployRole`, and leave the repository variable `AWS_DEPLOY_ENABLED` unset or `false`.

Keep `LIVE_MESSAGING_ENABLED=false`. Use the placeholder phone numbers and webhook public key from `.env.example` until Telnyx issues real ones. Do not put a Telnyx API key in the repository, the shell history, or GitHub.

Bootstrap CDK with a CloudFormation execution policy limited to this app’s resources. Do not attach `AdministratorAccess` to that execution role. The default CDK bootstrap qualifier stays `hnb659fds`, so a later GitHub role can still match the role names in `infrastructure/github-oidc-role.yaml` if this decision is revisited.

## Consequences

- A bad GitHub commit cannot change this AWS account, because GitHub has no AWS role here.
- Deploys happen only while someone is signed in on this PC with `aws login`. The session lasts up to 12 hours and then must be renewed.
- The project spend limit remains the cost backstop. Set a budget alert as well before leaving resources running.
- The [GitHub OIDC deployment guide](../deployment.md) stays the documented path for a later full account. It is not the path for this project.
- Revisit GitHub deploys only with an explicit decision to upgrade and activate advanced features.

## Implementation

The checked-in policy uses the example account `123456789012`. Replace it with the account you deploy to.

1. `HouseholdBudgetCfnExecution` should be the only policy on `cdk-hnb659fds-cfn-exec-role-123456789012-us-east-2`. It allows this stack’s DynamoDB, SQS, Lambda, API Gateway, CloudWatch Logs, and IAM roles, plus read of the CDK asset bucket and bootstrap version parameter. It does not include `AdministratorAccess`.
2. Bootstrap CDK in that account and region. The asset bucket should block public access and use a KMS key. No other account should be trusted to deploy.
3. Leave `LIVE_MESSAGING_ENABLED=false` until Discord settings replace the placeholders in `.env.example`.

[Decision 002](002-discord-channel.md) replaces the earlier `POST /telnyx` path with `POST /discord`. That shape needs the execution policy in `infrastructure/cfn-execution-policy.json`, which includes the 8 AM schedule.

The policy is also shaped around how CloudFormation actually deploys this stack:

- **Size.** IAM rejects a managed policy over 6,144 characters, ignoring whitespace. `test_policy_fits_the_iam_size_limit` fails before a publish would. IAM keeps five versions, so a later publish may have to delete the oldest non-default version first.
- **No `StackRolePolicies`.** CloudFormation creates roles (`CreateRole`) and inline policies (`PutRolePolicy`) for this stack. It does not need a separate managed-policy statement for the application roles.
- **Narrowed attach and detach.** CloudFormation attaches only AWS's `AWSLambdaBasicExecutionRole` to the stack's roles, so `iam:AttachRolePolicy` and `iam:DetachRolePolicy` are limited to that policy by condition. Writing an inline policy on a `HouseholdBudget*` role is still possible without a permissions boundary, so add one before ever enabling GitHub deploys.
- **Optional features** (SNS, alarms, budget, the card-sync schedule, Lambda event invoke config, DynamoDB index ARNs) are scoped to `HouseholdBudget*` names and unused while those features are off.
