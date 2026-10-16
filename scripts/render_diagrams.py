"""Draw the README diagrams with the AWS architecture icons.

    python scripts/render_diagrams.py

Needs Node.js. The .mmd files stay readable: a node written as
{{lambda|Webhook|Signature and allowlist}} is drawn as the Lambda icon plus those two lines.
"""

import base64
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIAGRAMS = ROOT / "docs" / "diagrams"
ICONS = DIAGRAMS / "icons"
TOKEN = re.compile(r"\{\{([a-z0-9]+)\|([^|}]*)\|([^}]*)\}\}")


def _icon(name: str) -> str:
    path = ICONS / f"{name}.png"
    if not path.is_file():
        raise SystemExit(f"No icon for {name!r} at {path}")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"<img src='data:image/png;base64,{encoded}' width='68'/>"


def expand(source: str) -> str:
    def replace(match: re.Match[str]) -> str:
        icon, title, detail = match.group(1), match.group(2), match.group(3)
        return f"{_icon(icon)}<br/><b>{title}</b><br/>{detail}"

    return TOKEN.sub(replace, source)


def render(name: str) -> None:
    source = DIAGRAMS / f"{name}.mmd"
    expanded = expand(source.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as folder:
        chart = Path(folder) / f"{name}.mmd"
        chart.write_text(expanded, encoding="utf-8")
        npx = shutil.which("npx") or shutil.which("npx.cmd")
        if npx is None:
            raise SystemExit("Node.js npx is required to draw the diagrams.")
        subprocess.run(
            [
                npx,
                "--yes",
                "--package",
                "@mermaid-js/mermaid-cli@12.0.0",
                "mmdc",
                "-i",
                str(chart),
                "-o",
                str(DIAGRAMS / f"{name}.png"),
                "-b",
                "white",
                "-t",
                "neutral",
                "-s",
                "2",
                "-c",
                str(DIAGRAMS / "mermaid-config.json"),
            ],
            cwd=ROOT,
            check=True,
        )


def main() -> None:
    for name in ("message-flow", "daily-flow", "deployment-flow"):
        render(name)


if __name__ == "__main__":
    main()
