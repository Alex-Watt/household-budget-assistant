"""Build an arm64 Linux Lambda asset on Windows without Docker."""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    target = (ROOT / ".build" / "lambda").resolve()
    if not target.is_relative_to(ROOT.resolve()):
        raise ValueError("Lambda asset path must remain inside the repository")
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--quiet",
            "--only-binary=:all:",
            "--platform=manylinux2014_aarch64",
            "--implementation=cp",
            "--python-version=3.14",
            "--target",
            str(target),
            "-r",
            str(ROOT / "requirements-runtime.txt"),
        ],
        check=True,
    )
    shutil.copytree(
        ROOT / "src" / "budget_bot",
        target / "budget_bot",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    print(f"Built Lambda asset: {target}")


if __name__ == "__main__":
    main()
