"""Run the same local checks as CI. --synth adds packaging and offline CDK synth."""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]


def run(arguments: list[str], env: dict[str, str] | None = None) -> None:
    print(f"Checking: {' '.join(arguments)}", flush=True)
    subprocess.run(arguments, cwd=ROOT, env=env, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--synth",
        action="store_true",
        help="Build Linux Lambda asset and synthesize with fake settings",
    )
    args = parser.parse_args()
    python = sys.executable
    run([python, "-m", "ruff", "check", "."])
    run([python, "-m", "ruff", "format", "--check", "."])
    run([python, "-m", "mypy"])
    run([python, "-m", "pytest"])
    if args.synth:
        run([python, "scripts/build_lambda.py"])
        npx = shutil.which("npx")
        if not npx or not (ROOT / "node_modules" / "aws-cdk").is_dir():
            raise SystemExit("Install Node.js and run npm ci before using --synth")
        env = {
            **os.environ,
            "PATH": str(Path(python).parent) + os.pathsep + os.environ.get("PATH", ""),
            "DISCORD_APPLICATION_ID": "100000000000000001",
            # Throwaway key: nobody holds its private half, so a mistaken deploy accepts nothing.
            "DISCORD_PUBLIC_KEY": Ed25519PrivateKey.generate()
            .public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            .hex(),
            "DISCORD_GUILD_ID": "100000000000000002",
            "DISCORD_CHANNEL_ID": "100000000000000003",
            "DISCORD_MEMBER_1_ID": "100000000000000004",
            "DISCORD_MEMBER_2_ID": "100000000000000005",
            "DISCORD_SECRET_NAME": "household-budget/discord",
            "LIVE_MESSAGING_ENABLED": "false",
            # Synthesize every optional resource so CI validates the whole stack.
            "AI_ENABLED": "true",
            "PLAID_ENABLED": "true",
            "ALERT_EMAIL": "alerts@example.com",
            "CDK_DEFAULT_ACCOUNT": "000000000000",
            "CDK_DEFAULT_REGION": "us-east-2",
            "AWS_EC2_METADATA_DISABLED": "true",
        }
        run([npx, "--no-install", "cdk", "synth", "--quiet", "--no-lookups"], env)


if __name__ == "__main__":
    main()
