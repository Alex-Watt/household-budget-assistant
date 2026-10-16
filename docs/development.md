# Development

[Back to README](../README.md)

## Prerequisites and first run

Use Python 3.14 to match Lambda and CI; newer local Python versions may work but are not the deployment runtime. Use Node.js 24/npm for CDK. AWS CLI is only needed for approved account setup and deployment, not local checks. Deploys from this PC follow [Discord setup](discord-setup.md).

Run from the repository root in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
npm ci
.\.venv\Scripts\python.exe scripts/check.py --synth
```

On Linux/macOS use `.venv/bin/python` instead. The Python package is installed in editable mode, so changes under `src/` are immediately importable. Tests do not manipulate `sys.path`. Generated assets live under `.build/` and `cdk.out/` and are ignored by Git.

## Daily workflow

```powershell
# Fast checks: lint, formatting, types, unit and local integration tests
.\.venv\Scripts\python.exe scripts/check.py

# Apply formatting / safe import fixes
.\.venv\Scripts\python.exe -m ruff check . --fix
.\.venv\Scripts\python.exe -m ruff format .

# Focused tests
.\.venv\Scripts\python.exe -m pytest tests/unit/test_messaging.py
.\.venv\Scripts\python.exe -m pytest tests/integration/test_state.py

# Before pushing infrastructure or dependency changes
.\.venv\Scripts\python.exe scripts/check.py --synth
```

The full check uses fake Discord IDs, a throwaway public key, and fake account settings. It forces dry-run messaging, disables CDK lookups, and never deploys. Package installation and building need internet access; tests themselves block unmocked AWS and Discord HTTP calls. Local integration tests use Moto's DynamoDB emulator and synthesized CDK assertions, not a deployed AWS integration.

## Configuration

`Settings` in `src/budget_bot/config.py` validates settings for both infrastructure and runtime. `.env.example` is documentation; no dotenv file is loaded automatically.

| Variable | Purpose | Default / required |
|---|---|---|
| `DISCORD_APPLICATION_ID` | The Discord application's ID | Required |
| `DISCORD_PUBLIC_KEY` | The application's 64-character hex Ed25519 key; public, not a secret | Required; small-order keys rejected |
| `DISCORD_GUILD_ID`, `DISCORD_CHANNEL_ID` | The household server and budget channel | Required |
| `DISCORD_MEMBER_1_ID` | The first household member's user ID | Required |
| `DISCORD_MEMBER_2_ID` | The second household member's user ID | Optional until they join; must differ from the first |
| `DISCORD_SECRET_NAME` | Secrets Manager secret whose value is the channel webhook URL | `household-budget/discord` |
| `LIVE_MESSAGING_ENABLED` | Allow replies and the daily post | `false`; only `true` or `false` accepted |
| `PLAID_ENABLED` | Create the Plaid sync and webhook Lambdas, schedule and route | `false` |
| `PLAID_SECRET_NAME` | Secrets Manager secret holding Plaid keys and read-only tokens | `household-budget/plaid` |
| `AI_ENABLED` | Let the worker call Claude Sonnet on Bedrock (adds the IAM permission) | `false` |
| `AI_DAILY_LIMIT` | AI answers allowed per Central day | `30`; 1 to 500 |
| `AI_PROFILE_SCOPE` | `us` keeps model traffic in the United States; `global` may route anywhere | `us` |
| `ALERT_EMAIL` | Creates alarms, an SNS email topic and a monthly cost budget | unset (none created) |
| `MONTHLY_COST_ALERT_USD` | Budget amount used by the cost alert | `50` |
| `QUEUE_URL`, `STATE_TABLE`, `LEDGER_TABLE`, `SYNC_FUNCTION` | Resource names the Lambdas use | Set by CDK |

Discord IDs are identifiers, not secrets. Never put the channel webhook URL, bot token, Plaid keys or tokens, payloads, transactions, or credentials into fixtures, committed configuration, or logs. The webhook URL and Plaid secrets belong in Secrets Manager; the bot token and Plaid secret are entered at hidden prompts by `scripts/register_commands.py` and `scripts/plaid_link.py` and not stored on disk. Lambda environment values are configuration, visible to identities permitted to read the function configuration.

## Scripts

| Script | Purpose | Touches AWS or the network? |
|---|---|---|
| `scripts/check.py` | Lint, format, types, tests; `--synth` also builds the Lambda asset and runs an offline `cdk synth` with every optional feature on | No (downloads wheels with `--synth`) |
| `scripts/deploy.py` | Build, `cdk diff`, then deploy, carrying over every live setting; flags change only what they name | AWS (read), then CloudFormation after you confirm |
| `scripts/demo.py` | Prints the 8 AM report and command answers from synthetic data | No |
| `scripts/ledger_admin.py` | `report`, `ask`, `audit`, `purge --feed` against the real ledger | AWS (read; `purge` deletes after confirmation) |
| `scripts/plaid_link.py` | `init`, `add`, `status`, `remove` for Plaid | Plaid and AWS Secrets Manager |
| `scripts/import_csv.py` | Preview or import a card CSV | AWS only with `--apply` |
| `scripts/check_ai_access.py` | Which Sonnet versions this project can call | Bedrock (a few tokens each) |
| `scripts/register_commands.py` | Register `/ask` | Discord |

## Dependency ownership

- `requirements-runtime.txt`: Lambda libraries; read by package installation and Linux asset build.
- `requirements.txt`: CDK dependencies and editable application install.
- `requirements-dev.txt`: tests, linting, types, and local AWS emulation.
- `package.json` / `package-lock.json`: the pinned CDK CLI. Use `npm ci` for repeatable installation.
- `pyproject.toml`: package metadata and shared pytest/Ruff/mypy settings. Mypy checks our modules while skipping external library implementations to keep CDK analysis bounded.

Direct Python dependencies are pinned; transitive Python dependencies are not fully locked. Review dependency updates and run the full check before merging. The Lambda builder downloads Linux arm64 (Graviton) Python 3.14 wheels and excludes local bytecode. It is import-safe and can be run directly with `python scripts/build_lambda.py`.

## Change boundaries

Keep Lambda entry points thin. Money arithmetic belongs in `budget.py` and nowhere else: the model, `render.py` and the Lambdas must only format numbers that `budget.py` computed. Provider details stay in their adapters (`plaid.py`, `discord.py`, `llm.py`), persistence in `ledger.py` and `state.py`, and category knowledge in `categories.py`. Add tests beside the behavior being changed, and when a new store or provider is added, check it gives the same answers as the in-memory one.

When a tool is added for the model, validate every argument, return pre-formatted money strings, keep it read-only unless it is audited and reversible, and add a test that hostile merchant text cannot cause a write. When a resource is added to the stack, extend `infrastructure/cfn-execution-policy.json`; `tests/integration/test_execution_policy.py` fails if a created IAM policy or schedule is not covered.

Update the [architecture](architecture.md), [roadmap](roadmap.md), and [diagram sources/images](diagrams/README.md) when behavior or resources change. Use a focused commit/PR with the reason for the change, verification results, and any remaining live-test limitation. Live Plaid and Bedrock calls need explicit approval; local checks never make them.

On Windows PowerShell 5, write files with an editor or `-Encoding utf8`: `Set-Content` defaults to the legacy code page and corrupts characters such as the em dash.
