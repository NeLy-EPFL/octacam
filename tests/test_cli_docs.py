"""`docs/cli.md`, the command line's reference, is generated from the command line.

Regenerate it with `uv run python tests/test_cli_docs.py`.
"""

import subprocess
import sys
from pathlib import Path

DOCS = Path(__file__).parents[1] / "docs" / "cli.md"


def generate() -> str:
    """The reference as Typer's doc generator writes it, a paragraph to a line."""
    argv = ["typer", "octacam.cli", "utils", "docs", "--name", "octacam"]
    out = subprocess.run(
        [sys.executable, "-m", *argv, "--title", "CLI reference"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    lines, fence, joinable = [], False, False
    for line in out.splitlines():
        if line.startswith("```"):
            fence = not fence
            lines.append(line)
            joinable = False
            continue
        prose = bool(line) and not fence and not line.startswith(("#", "*", "|"))
        if prose and joinable:
            lines[-1] += " " + line
        else:
            lines.append(line)
        joinable = prose
    return "\n".join(lines).strip() + "\n"


def test_the_reference_is_current():
    assert DOCS.read_text(encoding="utf-8") == generate(), (
        "docs/cli.md is stale: uv run python tests/test_cli_docs.py"
    )


if __name__ == "__main__":
    DOCS.write_text(generate(), encoding="utf-8")
