# Discord setup and live test

[Back to README](../README.md) · [Decision](decisions/002-discord-channel.md)

The bot lives in one private Discord channel. Either spouse types `/ask` there, and the reply shows up in the same channel. Every day at 8:00 AM Central, the bot posts an update in that channel. Discord costs nothing.

Discord messages are not end-to-end encrypted, and Discord stores them. Both spouses should agree to spending summaries appearing there before going live.

## 1. Server and channel

1. In Discord, create a server (**+** → **Create My Own**) and invite your spouse. Nobody else should join.
2. Create one text channel, for example `#budget`.
3. Turn on two-factor authentication for both Discord accounts.

## 2. Discord application

1. Open the [Developer Portal](https://discord.com/developers/applications) and choose **New Application**. Name it, for example, *Household Budget*.
2. On **General Information**, copy the **Application ID** and **Public Key**. The public key is not a secret.
3. On **Bot**, turn **Public Bot** off so nobody else can add it to their server.
4. On **OAuth2** → **URL Generator**, select only the `applications.commands` scope. Open the generated URL and add the app to your server.

## 3. IDs

In Discord, turn on **User Settings** → **Advanced** → **Developer Mode**. Then right-click to copy:

- the server, giving its **Server ID**
- `#budget`, giving its **Channel ID**
- each person who should be allowed to use the bot, giving their **User ID**. One ID is enough to start; add the second when they join.

These are identifiers, not passwords.

## 4. Channel webhook for the 8 AM update

In `#budget` → **Edit Channel** → **Integrations** → **Webhooks**, create a webhook named *Budget Bot* and copy its URL. Treat that URL like a password: anyone who has it can post in the channel. Don't paste it in chat, Git, or `.env` files.

Store it in AWS from this PC. The prompt hides what you paste:

```powershell
$secure = Read-Host "Discord channel webhook URL" -AsSecureString
$url = [System.Net.NetworkCredential]::new("", $secure).Password
aws secretsmanager create-secret --region us-east-2 --name household-budget/discord --secret-string $url
Remove-Variable url, secure
```

Only the report Lambda can read this secret. One secret costs about $0.40 a month.

**Rotating the URL.** Anyone who has this URL can post in the channel as the bot, though not read anything. If it was ever pasted into a chat, an email or a file, replace it: in `#budget` → **Edit Channel** → **Integrations** → **Webhooks**, delete *Budget Bot* and create a new one, then store the new URL (the old one stops working immediately):

```powershell
$secure = Read-Host "New Discord channel webhook URL" -AsSecureString
$url = [System.Net.NetworkCredential]::new("", $secure).Password
aws secretsmanager put-secret-value --region us-east-2 --secret-id household-budget/discord --secret-string $url
Remove-Variable url, secure
```

The report Lambda caches the URL while it is warm, so the new one is used from the next cold start, in practice the next 8 AM post.

## 5. Deploy from this PC

This follows [decision 001](decisions/001-local-aws-deploy.md): the sample AWS project, `us-east-2`, with `aws login` credentials. If the scoped CloudFormation policy changed, publish it first. IAM keeps five versions, so delete the oldest non-default version if that call fails; it also rejects a policy over 6,144 characters, which a test checks.

```powershell
aws iam create-policy-version --policy-arn arn:aws:iam::123456789012:policy/HouseholdBudgetCfnExecution --policy-document file://infrastructure/cfn-execution-policy.json --set-as-default
```

Then deploy with live messaging off:

```powershell
$env:DISCORD_APPLICATION_ID = '<application id>'
$env:DISCORD_PUBLIC_KEY = '<public key>'
$env:DISCORD_GUILD_ID = '<server id>'
$env:DISCORD_CHANNEL_ID = '<budget channel id>'
$env:DISCORD_MEMBER_1_ID = '<spouse 1 user id>'
# Set this when the second person joins. Leave it unset until then.
# $env:DISCORD_MEMBER_2_ID = '<spouse 2 user id>'
$env:LIVE_MESSAGING_ENABLED = 'false'
$env:CDK_DEFAULT_ACCOUNT = '123456789012'
$env:CDK_DEFAULT_REGION = 'us-east-2'
$env:PATH = "$PWD\.venv\Scripts;$env:PATH"
python scripts/build_lambda.py
npx --no-install cdk deploy HouseholdBudgetMessaging --require-approval never
```

Configuration validation rejects the `.env.example` placeholders, so a deploy with missing values fails before anything changes. Copy the `InteractionsUrl` output.

**Every deploy after the first:** use the helper instead of retyping those variables. It reads the running stack (Discord IDs, live messaging, Plaid, AI and alert settings), shows the plan and the `cdk diff`, and asks before deploying. A flag changes only the setting it names; if it cannot read the running stack it stops rather than falling back to defaults, because the default for live messaging is off and would pause the bot.

```powershell
python scripts/deploy.py                              # preview and ask
python scripts/deploy.py --alert-email you@example.com
python scripts/deploy.py --plaid on --yes             # no prompt
python scripts/deploy.py --preview                    # stop after the diff
```

The stack has termination protection on, so `cdk destroy` or deleting it in the console is refused until you turn that off in the CloudFormation console. The data tables are also retained if the stack is ever deleted.

## 6. Connect Discord to AWS

1. In the Developer Portal → **General Information**, paste `InteractionsUrl` into **Interactions Endpoint URL** and save. Discord sends a signed test request; the save fails if the endpoint doesn't verify it.
2. Register `/ask`. On **Bot**, choose **Reset Token**, then paste the token at the hidden prompt. It is used once and not saved:

```powershell
python scripts/register_commands.py
```

3. Optional: in **Server Settings** → **Integrations** → your app, limit `/ask` to `#budget` and the two of you. The code enforces this regardless.

## 7. Dry-run check

Each of you types `/ask help` in `#budget`. You should each see a private dry-run notice. Nothing posts publicly, and the 8 AM job records a dry run without posting.

## 8. Go live

Redeploy with `LIVE_MESSAGING_ENABLED = 'true'` and confirm the `LiveMessaging` output is `true`.

**Go** when all of these hold:

1. Each spouse's `/ask help` gets a reply in `#budget` that both of you can see.
2. `/ask` from another channel, or from anyone else, gets a private refusal.
3. The next 8:00 AM Central update appears once.
4. CloudWatch logs show no errors and the dead-letter queue is empty.

To see an update before 8 AM, invoke the report Lambda once from the AWS console or CLI. That uses up today's post, so the 8 AM run that day records a duplicate and stays quiet.

To pause, redeploy with `LIVE_MESSAGING_ENABLED = 'false'`.

## 9. Next: card data and AI

After the go/no-go, follow [Banking and AI setup](banking-and-ai-setup.md) to connect the cards through Plaid (or a CSV) and turn on Claude Sonnet. Until then the 8 AM update and `/ask` say that card data is not connected. The Plaid, AI and alert settings are additional environment variables on the same `cdk deploy`.
