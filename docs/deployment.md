# Single-account AWS deployment

[Back to README](../README.md)

## GitHub Actions CI/CD to one AWS account

`.github/workflows/ci-cd.yml` runs tests, Ruff, mypy, Lambda packaging, and `cdk synth` on pull requests. After the AWS setup below and explicit enablement, pushes to `main` deploy the stack to **one account and one region**. The workflow uses GitHub OIDC temporary credentials; it does not store AWS access keys. It pins GitHub Actions to release commit SHAs and rejects AWS credentials from any account except `AWS_ACCOUNT_ID`. Deploys use the GitHub Environment `aws-production`, restricted to `main`.

**Current decision:** deploy from your own machine. GitHub Actions deployment below stays off until you set it up and turn it on. See [the accepted decision](decisions/001-local-aws-deploy.md) and [Discord setup](discord-setup.md). The sample targets one region, such as `us-east-2`. Messaging, Plaid, and AI default to off. GitHub Actions never turns those features on by itself: the workflow forces them off, the same way it forces live messaging off.

### 1. Choose the target account and region

Use the same AWS account and region for the OIDC role, CDK bootstrap, and app deployment. The project can use any region where the listed services are available; set one value, such as `us-east-2`, as `AWS_REGION`. Get the 12-digit ID for the AWS account where the app will live.

### 2. Create the GitHub OIDC role in AWS

Sign in to the intended AWS account with an administrator identity, then create a CloudFormation stack from [infrastructure/github-oidc-role.yaml](../infrastructure/github-oidc-role.yaml) in the chosen region. The template is pinned to this GitHub repository using immutable owner/repository IDs and the `aws-production` environment. It creates the GitHub OIDC provider if absent and a role limited to assuming the four standard CDK bootstrap roles for this account and region.

Before creating the stack, check IAM for an existing provider with URL `https://token.actions.githubusercontent.com`. If it exists, pass its ARN as `ExistingGitHubOidcProviderArn` in the stack parameters; IAM allows only one provider per URL per account. The template defaults match `Alex-Watt/household-budget-assistant` and its immutable IDs. If you fork or rename the repository, update the owner and repository names and IDs to match that repo.

After the stack completes, copy its `GitHubActionsDeployRoleArn` output. The CloudFormation template creates IAM resources only; it does not deploy the application.

### 3. Bootstrap CDK once in the same account and region

Install the [local prerequisites](development.md) and run `npm ci` first. Use AWS CLI with an approved administrator profile.

Using an approved local AWS administrator profile, run once:

```powershell
$accountId = '123456789012' # replace with your account ID
$region = 'us-east-2'       # use the region selected above
npx --no-install cdk bootstrap "aws://$accountId/$region"
```

CDK's default bootstrap CloudFormation execution role has `AdministratorAccess` in that account. This is broad: a trusted deploy can create or change account resources through CloudFormation. Review the CDK security guidance and this permission before bootstrapping. The GitHub role itself can only assume the named CDK roles; the bootstrap execution role determines what deployed templates can change. No bootstrap command has been run from this workspace. [AWS CDK bootstrap documentation](https://docs.aws.amazon.com/cdk/v2/guide/ref-cli-cmd-bootstrap.html), [CDK security best practices](https://docs.aws.amazon.com/cdk/v2/guide/best-practices-security.html).

### 4. Set GitHub deployment configuration

In repository **Settings → Environments**, create `aws-production`. Restrict deployments to the `main` branch. For an extra approval step, add a required reviewer if the plan for this private repository supports environment reviewers.

In **Settings → Secrets and variables → Actions → Variables**, add a **repository variable** `AWS_DEPLOY_ENABLED` with value `false`. Change it to `true` only after deployment is approved and setup is complete. This is the deployment on/off switch.

The switch must be at repository scope because GitHub evaluates the job condition before environment variables are available. [GitHub variable availability](https://docs.github.com/en/actions/reference/workflows-and-actions/variables).

In the `aws-production` environment, add these variables:

| Name | Value |
|---|---|
| `AWS_ACCOUNT_ID` | The one target account's 12-digit ID |
| `AWS_REGION` | The one selected deployment region |
| `AWS_DEPLOY_ROLE_ARN` | The OIDC role ARN output from the CloudFormation stack |
| `DISCORD_APPLICATION_ID`, `DISCORD_PUBLIC_KEY` | From the Developer Portal's General Information page; the key is public |
| `DISCORD_GUILD_ID`, `DISCORD_CHANNEL_ID` | The household server and budget channel |
| `DISCORD_MEMBER_1_ID` | The first household member's user ID |
| `DISCORD_MEMBER_2_ID` | The second household member's user ID; omit until they join |

These are identifiers, not secrets; see [Discord setup](discord-setup.md#3-ids). The channel webhook URL stays in Secrets Manager and is never a GitHub value.

Replies and the daily post remain disabled because `LIVE_MESSAGING_ENABLED` defaults to `false`. The GitHub workflow does not enable live messaging. Do not add bank credentials to GitHub Actions.

### 5. Deploy through CI/CD

Once setup is complete and deployment is approved, set the repository variable `AWS_DEPLOY_ENABLED=true`. A successful push to `main` then runs CDK deploy to that account and region. While the switch is absent or `false`, the deploy job is skipped while CI still runs. The workflow validates the account, region, role ARN, and Discord configuration before requesting AWS credentials. `workflow_dispatch` can also start the deployment from `main`. Pull requests never receive AWS credentials.

This wires deployment, but it does not create AWS resources now. Before allowing production deployments, decide whether you accept the default CDK account-wide CloudFormation permissions or want a narrower custom execution policy. An approved reviewer gate on the GitHub Environment adds a human checkpoint when available on the repository plan.

## Deployment preparation — requires separate approval

Follow [Discord setup](discord-setup.md): create the private server and application, store the channel webhook URL as the `household-budget/discord` secret, deploy with live messaging off, then connect Discord and register `/ask`. Live mode needs both spouses' consent and a stack update with `LIVE_MESSAGING_ENABLED=true`. Check CloudWatch errors and the SQS dead-letter queue during the live test, and set AWS budgets or alarms before ongoing use.

## Operations and recovery

- Disable further deployments: set repository variable `AWS_DEPLOY_ENABLED=false`. This does not stop resources already running.
- The standard workflow always sets `LIVE_MESSAGING_ENABLED=false`. An approved live test requires a reviewed workflow/configuration change. Subsequent normal deployments restore dry-run mode.
- Inspect each Lambda's CloudWatch log group (30-day event retention). Logs include outcome or error type only, with no question text, user ID, or token. Follow the `InteractionsUrl` and `LiveMessaging` CloudFormation outputs.
- Investigate failed events in the SQS dead-letter queue before redriving. Interaction tokens expire after 15 minutes, so old questions cannot be answered by a replay.
- Revert a bad commit and let approved CI/CD redeploy. Review `cdk diff` before infrastructure changes. A Git revert cannot undo messages already sent or recover deleted data.
- DynamoDB has point-in-time recovery and is retained on stack deletion. Log groups are also retained; retained resources may continue to incur costs. Do not delete the stack or retained data as a routine rollback.
- GitHub settings are not a substitute for household consent, AWS cost limits, or the [Discord go/no-go test](discord-setup.md#8-go-live). Set budgets/alarms before ongoing use; alarm resources are not implemented yet.
