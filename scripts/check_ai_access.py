"""Check which Claude Sonnet versions this AWS project can call. Needs `aws login`.

    python scripts/check_ai_access.py
    python scripts/check_ai_access.py --scope global

Sends one tiny message (a few tokens, a fraction of a cent) to each Sonnet version from
highest to lowest and stops at the first that works. That is the model the bot will use.
"""

import argparse
import sys
from typing import Any

import boto3
from botocore.exceptions import ClientError

from budget_bot.aws_login import sign_in_or_exit
from budget_bot.llm import BedrockSonnet, sonnet_candidates


def probe(runtime: Any, control: Any, scope: str) -> list[tuple[str, str]]:
    """(model, result) for each candidate until one works; result is "works" or an error code."""
    ids: list[str] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"typeEquals": "SYSTEM_DEFINED", "maxResults": 1000}
        if token:
            kwargs["nextToken"] = token
        page = control.list_inference_profiles(**kwargs)
        ids += [p["inferenceProfileId"] for p in page.get("inferenceProfileSummaries", [])]
        token = page.get("nextToken")
        if not token:
            break
    results: list[tuple[str, str]] = []
    for model_id in sonnet_candidates(ids, scope):
        try:
            runtime.converse(
                modelId=model_id,
                messages=[{"role": "user", "content": [{"text": "Reply with: ok"}]}],
                inferenceConfig={"maxTokens": 16},
            )
        except ClientError as exc:
            results.append((model_id, exc.response["Error"]["Code"]))
            continue
        results.append((model_id, "works"))
        break
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=["us", "global"], default="us")
    parser.add_argument("--region", default="us-east-2")
    args = parser.parse_args()
    sign_in_or_exit()
    runtime = boto3.client("bedrock-runtime", region_name=args.region)
    control = boto3.client("bedrock", region_name=args.region)
    results = probe(runtime, control, args.scope)
    if not results:
        sys.exit("No Claude Sonnet inference profiles are listed for this project.")
    for model_id, result in results:
        print(f"{model_id}: {result}")
    if results[-1][1] != "works":
        print(
            "\nNo Sonnet version is usable yet. AccessDeniedException ('not available for this "
            "account') is an account-level limit; ResourceNotFoundException usually means the "
            "Anthropic use-case form has not been submitted in the Bedrock console."
        )
        sys.exit(1)
    best = BedrockSonnet(runtime, control, args.scope).candidates()[0]
    note = "" if best == results[-1][0] else f" (the newest listed, {best}, is not enabled yet)"
    print(f"\nThe bot will use {results[-1][0]}{note}.")


if __name__ == "__main__":
    main()
