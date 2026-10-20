"""Guard the ways `atlas/update.py` is launched.

`atlas/types.py` shadows the stdlib `types` module whenever the `atlas/`
directory itself is `sys.path[0]`, which is exactly what happens for
`python atlas/update.py`. Whether that crashes depends on whether the
interpreter has already imported `types` at startup, which varies with the
uv/venv version (the daily-update workflow broke when uv moved to 0.13.0).
Launch the updater as a module so `atlas/` never lands on `sys.path`.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# `python atlas/update.py`, `python3 atlas/update.py`, `uv run python atlas/update.py`
SCRIPT_PATH_INVOCATION = re.compile(r"\bpython3?\s+atlas/\w+\.py\b")

LAUNCHER_FILES = [
    *sorted((REPO_ROOT / ".github" / "workflows").glob("*.y*ml")),
    REPO_ROOT / "README.md",
]


@pytest.mark.parametrize("path", LAUNCHER_FILES, ids=lambda p: p.name)
def test_atlas_scripts_are_not_launched_by_file_path(path: Path):
    offenders = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if SCRIPT_PATH_INVOCATION.search(line)
    ]

    assert not offenders, (
        "launch with `python -m atlas.<module>` instead, so atlas/types.py "
        f"cannot shadow the stdlib `types` module: {offenders}"
    )


def test_update_cli_runs_as_a_module():
    result = subprocess.run(
        [sys.executable, "-m", "atlas.update", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--accept-missing" in result.stdout
    assert "--source" not in result.stdout
