"""Connect a card to Plaid from this PC. Secrets go straight to AWS Secrets Manager.

    python scripts/plaid_link.py init                  # store Plaid client ID and secret (hidden)
    python scripts/plaid_link.py init --from-file keysecret.txt --delete-file   # same, from a 2-line file
    python scripts/plaid_link.py add --sandbox         # rehearse with Plaid's fake bank, no real data
    python scripts/plaid_link.py update --item-id <id>   # re-link an expired login, same connection
    python scripts/plaid_link.py status
    python scripts/plaid_link.py remove --item-id <id>

Requires `aws login` and the household's explicit approval before any real card is linked.
Access tokens are never printed. Linking is read-only: only the Transactions product is asked for.
"""

import argparse
import getpass
import json
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import boto3
from botocore.exceptions import ClientError

from budget_bot.accounts import KNOWN_ACCOUNTS
from budget_bot.aws_login import sign_in_or_exit
from budget_bot.credentials import PLAID_ENVIRONMENTS, parse_plaid_config
from budget_bot.ledger import DynamoLedger, ledger_table
from budget_bot.plaid import PlaidClient, PlaidError

SANDBOX_INSTITUTION = "ins_109508"  # Plaid's "First Platypus Bank" test institution
POLL_SECONDS = 5
LINK_TIMEOUT_SECONDS = 900


class SecretStore(Protocol):
    def load(self) -> dict[str, Any] | None: ...

    def save(self, value: dict[str, Any]) -> None: ...


class AwsSecret:
    def __init__(self, name: str) -> None:
        self.name = name
        self.client = boto3.client("secretsmanager")

    def load(self) -> dict[str, Any] | None:
        try:
            value = self.client.get_secret_value(SecretId=self.name)["SecretString"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                return None
            raise
        loaded: dict[str, Any] = json.loads(value)
        return loaded

    def save(self, value: dict[str, Any]) -> None:
        text = json.dumps(value)
        try:
            self.client.put_secret_value(SecretId=self.name, SecretString=text)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
            self.client.create_secret(Name=self.name, SecretString=text)


def init_secret(store: SecretStore, client_id: str, secret: str, environment: str) -> None:
    if environment not in PLAID_ENVIRONMENTS:
        raise ValueError("environment must be sandbox or production")
    existing = store.load() or {}
    if existing.get("items") and existing.get("environment") not in (None, environment):
        # Access tokens only work in the environment that issued them.
        raise ValueError("Remove the linked items before switching Plaid environments.")
    store.save(
        {
            "client_id": client_id,
            "secret": secret,
            "environment": environment,
            "items": existing.get("items", []),
        }
    )


KEY_LINE = re.compile(r"\s*(key|client_id|secret)\s*[:=]\s*(\S+)\s*", re.IGNORECASE)
CREDENTIAL = re.compile(r"[A-Za-z0-9]{16,64}")


def read_key_file(path: Path) -> tuple[str, str]:
    """(client_id, secret) from a file with a `key: ...` line and a `secret: ...` line.

    Error messages describe the problem but never repeat anything from the file.
    """
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        match = KEY_LINE.fullmatch(line)
        if match is None:
            raise ValueError("Each line must look like `key: ...` or `secret: ...`.")
        name = "secret" if match[1].lower() == "secret" else "client_id"
        if name in found:
            raise ValueError(f"The file gives the {name} twice.")
        found[name] = match[2]
    if set(found) != {"client_id", "secret"}:
        raise ValueError("The file needs one `key:` line and one `secret:` line.")
    if not all(CREDENTIAL.fullmatch(value) for value in found.values()):
        raise ValueError("The key or secret does not look like a Plaid credential.")
    return found["client_id"], found["secret"]


def sandbox_accepts(client: PlaidClient) -> bool:
    """Does Plaid's sandbox (fake data) accept these credentials? One harmless read."""
    try:
        client.call("/institutions/get", {"count": 1, "offset": 0, "country_codes": ["US"]})
    except PlaidError as exc:
        if exc.code == "INVALID_API_KEYS":
            return False
        raise
    return True


def init_from_file(
    store: SecretStore,
    path: Path,
    environment: str | None = None,
    client_factory: Callable[..., PlaidClient] = PlaidClient,
) -> str:
    """Store the credentials in the file and return the environment they were saved for.

    Without an explicit environment the sandbox is tried (it holds only fake data). Production
    is never contacted here: a secret the sandbox rejects needs `--environment production`.
    """
    client_id, secret = read_key_file(path)
    if environment is None:
        if not sandbox_accepts(client_factory("sandbox", client_id, secret)):
            raise ValueError(
                "Plaid's sandbox does not accept these credentials. If they are your "
                "production key and secret, run again with --environment production."
            )
        environment = "sandbox"
    init_secret(store, client_id, secret, environment)
    return environment


def public_token_from(session: dict[str, Any]) -> str | None:
    """The public token from a finished Hosted Link session, in either response shape."""
    results = (session.get("results") or {}).get("item_add_results") or []
    if results and results[0].get("public_token"):
        return str(results[0]["public_token"])
    token = (session.get("on_success") or {}).get("public_token")
    return str(token) if token else None


def wait_for_public_token(
    client: PlaidClient,
    link_token: str,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    timeout: float = LINK_TIMEOUT_SECONDS,
) -> str:
    deadline = clock() + timeout
    while clock() < deadline:
        for session in client.link_token_get(link_token).get("link_sessions") or []:
            token = public_token_from(session)
            if token:
                return token
        sleep(POLL_SECONDS)
    raise TimeoutError("The bank connection was not finished in time. Run the command again.")


def session_finished(session: dict[str, Any]) -> bool:
    """A Hosted Link session the person completed, including an update that issues no new token."""
    return bool(
        session.get("finished_at") or session.get("on_success") or public_token_from(session)
    )


def wait_for_link_finish(
    client: PlaidClient,
    link_token: str,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    timeout: float = LINK_TIMEOUT_SECONDS,
) -> None:
    deadline = clock() + timeout
    while clock() < deadline:
        for session in client.link_token_get(link_token).get("link_sessions") or []:
            if session_finished(session):
                return
        sleep(POLL_SECONDS)
    raise TimeoutError("The bank connection was not finished in time. Run the command again.")


def update_item(
    store: SecretStore,
    item_id: str,
    webhook_url: str | None,
    out: Callable[[str], None] = print,
    client_factory: Callable[..., PlaidClient] = PlaidClient,
) -> str:
    """Ask the person to sign in again for an existing connection. The access token stays."""
    config = store.load()
    match = [item for item in (config or {}).get("items", []) if item["item_id"] == item_id]
    if config is None or not match:
        raise ValueError("No such item.")
    client = client_factory(config["environment"], config["client_id"], config["secret"])
    created = client.link_token_create(
        "household", webhook_url, access_token=match[0]["access_token"]
    )
    out("Open this link and sign in again. This refreshes the same connection:")
    out(created["hosted_link_url"])
    out("Waiting for you to finish...")
    wait_for_link_finish(client, created["link_token"])
    return "That bank login was refreshed. The same connection and stored charges were kept."


def choose_labels(
    accounts: list[dict[str, Any]],
    taken: set[str],
    prompt: Callable[[str], str],
    out: Callable[[str], None],
) -> dict[str, str]:
    """Ask which household card each credit card is. Anything else on the login is ignored."""
    labels: dict[str, str] = {}
    options = [key for key in KNOWN_ACCOUNTS if key not in taken]
    for account in accounts:
        if account.get("type") != "credit":
            out(f"Skipping {account.get('subtype', 'account')} (not a credit card).")
            continue
        menu = ", ".join(f"{i}={key}" for i, key in enumerate(options, 1))
        answer = (
            prompt(
                f"Which card is '{account.get('name', 'card')}' ending {account.get('mask', '?')}? "
                f"[{menu}, s=skip]: "
            )
            .strip()
            .lower()
        )
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            key = options[int(answer) - 1]
            labels[account["account_id"]] = key
            options.remove(key)
        elif answer not in {"s", "skip", ""}:
            out("Not recognized, skipping that account.")
    return labels


def add_item(
    store: SecretStore,
    client: PlaidClient,
    public_token: str,
    prompt: Callable[[str], str],
    out: Callable[[str], None],
) -> str:
    config = store.load()
    if config is None:
        raise ValueError("Run `init` first to store the Plaid client ID and secret.")
    exchanged = client.public_token_exchange(public_token)
    access_token, item_id = exchanged["access_token"], exchanged["item_id"]
    saved = False
    try:
        taken = {key for item in config["items"] for key in item["accounts"].values()}
        labels = choose_labels(client.accounts_get(access_token)["accounts"], taken, prompt, out)
        if not labels:
            raise ValueError("No cards were selected, so the connection was removed.")
        item = {
            "item_id": item_id,
            "access_token": access_token,
            "institution": "Plaid",
            "accounts": labels,
        }
        config["items"].append(item)
        parse_plaid_config(json.dumps(config))  # refuse to save anything the runtime would reject
        store.save(config)
        saved = True
    finally:
        if not saved:
            # Whatever went wrong (no selection, a failed save, Ctrl-C), do not leave an
            # authorized connection at Plaid that no stored token can ever remove.
            client.item_remove(access_token)
    return str(item_id)


def remove_item(
    store: SecretStore,
    client: PlaidClient,
    item_id: str | None,
    purge: Callable[[str], int] | None = None,
) -> str:
    """Disconnect one bank login. A new connection to the same card gets new transaction IDs,
    so its old transactions should be purged or the same purchases would be counted twice."""
    config = store.load()
    match = [i for i in (config or {}).get("items", []) if i["item_id"] == item_id]
    if config is None or not match:
        raise ValueError("No such item.")
    try:
        client.item_remove(match[0]["access_token"])
    except PlaidError as exc:
        # Already gone at Plaid's end: still take it out of the secret.
        if exc.code not in {"INVALID_ACCESS_TOKEN", "ITEM_NOT_FOUND"}:
            raise
    config["items"] = [i for i in config["items"] if i["item_id"] != item_id]
    store.save(config)
    message = "Removed from Plaid and from the secret."
    if purge is not None:
        message += f" Deleted {purge(f'plaid:{item_id}')} stored transactions."
    else:
        message += (
            " Its stored transactions remain; if you are re-linking this card, run "
            f"`ledger_admin.py purge --feed plaid:{item_id}` first so purchases are not counted twice."
        )
    return message


def link(
    store: SecretStore,
    sandbox: bool,
    webhook_url: str | None,
    prompt: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
    client_factory: Callable[..., PlaidClient] = PlaidClient,
) -> str:
    config = store.load()
    if config is None:
        raise ValueError("Run `init` first to store the Plaid client ID and secret.")
    client = client_factory(config["environment"], config["client_id"], config["secret"])
    if sandbox:
        if config["environment"] != "sandbox":
            raise ValueError("--sandbox needs the secret to be in the sandbox environment")
        public_token = client.sandbox_public_token(SANDBOX_INSTITUTION)["public_token"]
        out("Using Plaid's fake test bank (sign-in is user_good / pass_good if asked).")
    else:
        created = client.link_token_create("household", webhook_url)
        out("Open this link, choose the bank and sign in. Plaid shows the login, not this app:")
        out(created["hosted_link_url"])
        out("Waiting for you to finish...")
        public_token = wait_for_public_token(client, created["link_token"])
    return add_item(store, client, public_token, prompt, out)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("command", choices=["init", "add", "update", "status", "remove"])
    parser.add_argument("--secret-name", default="household-budget/plaid")
    parser.add_argument("--sandbox", action="store_true")
    parser.add_argument(
        "--from-file",
        type=Path,
        help="init: read `key: ...` and `secret: ...` lines from this file instead of prompting",
    )
    parser.add_argument(
        "--environment",
        choices=sorted(PLAID_ENVIRONMENTS),
        help="init: skip the sandbox check and store the credentials for this environment",
    )
    parser.add_argument(
        "--delete-file",
        action="store_true",
        help="init --from-file: delete the file once the credentials are safely stored",
    )
    parser.add_argument("--webhook-url")
    parser.add_argument("--item-id")
    parser.add_argument(
        "--table",
        default=os.environ.get("LEDGER_TABLE", ""),
        help="Ledger table; lets `remove` offer to delete the card's stored transactions",
    )
    args = parser.parse_args()
    sign_in_or_exit()
    store = AwsSecret(args.secret_name)
    if args.command == "init" and args.from_file:
        try:
            env = init_from_file(store, args.from_file, args.environment)
        except ValueError as exc:
            sys.exit(str(exc))
        except PlaidError as exc:
            sys.exit(f"Plaid check failed ({exc.code}); nothing was saved.")
        except OSError:
            sys.exit("Could not read that file; nothing was saved.")
        print(f"Saved to Secrets Manager (environment: {env}).")
        if args.delete_file:
            try:
                args.from_file.unlink()
                print("Deleted the credentials file.")
            except OSError:
                print("Could not delete the credentials file. Please delete it yourself.")
    elif args.command == "init":
        env = args.environment or input("Plaid environment (sandbox or production): ").strip()
        init_secret(
            store,
            input("Plaid client_id: ").strip(),
            getpass.getpass("Plaid secret (hidden): ").strip(),
            env,
        )
        print("Saved to Secrets Manager.")
    elif args.command == "add":
        print(
            f"Linked item {link(store, args.sandbox, args.webhook_url)}. It syncs on the next run."
        )
    elif args.command == "update":
        if not args.item_id:
            sys.exit("Pass --item-id for the connection to refresh. `status` lists them.")
        try:
            print(update_item(store, args.item_id, args.webhook_url))
        except (ValueError, TimeoutError, PlaidError) as exc:
            sys.exit(str(getattr(exc, "code", exc)))
    elif args.command == "status":
        linked = (store.load() or {"items": []})["items"]
        for item in linked:
            print(item["item_id"], sorted(item["accounts"].values()))
        print(f"{len(linked)} item(s) linked.")
    else:
        config = store.load()
        if config is None:
            sys.exit("Run `init` first.")
        client = PlaidClient(config["environment"], config["client_id"], config["secret"])
        purge: Callable[[str], int] | None = None
        if (
            args.table
            and input("Also delete this card's stored transactions? [y/N]: ").lower() == "y"
        ):
            purge = DynamoLedger(ledger_table(args.table)).purge_feed
        try:
            print(remove_item(store, client, args.item_id, purge))
        except ValueError as exc:
            sys.exit(str(exc))


if __name__ == "__main__":
    main()
