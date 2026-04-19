"""Run the project's semgrep ruleset as part of pytest.

Rules live in ``.semgrep/`` in the repo root.  Today they enforce:
- FMEntries construction (plain-dict frontmatter entries are banned)
- SSRF precedence (outbound fetch must be preceded by check_url_ssrf)
- Content fencing (no hand-rolled ┌─/└─ markers outside markdown.py)

semgrep is pinned to ``>=1.140.0`` in the dev dep group — that's the
first release to bump ``ruamel.yaml.clib`` to 0.2.14 and pass
Python 3.14 CI (see semgrep #11250).
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest


_REPO_ROOT = pathlib.Path(__file__).parent.parent
_SEMGREP_RULES = _REPO_ROOT / ".semgrep"


def _semgrep_available() -> bool:
    return shutil.which("semgrep") is not None


@pytest.mark.skipif(not _semgrep_available(), reason="semgrep not installed; skipping rules gate")
def test_semgrep_rules_pass():
    """All project-specific semgrep rules produce zero findings on the
    current tree.  A failure here flags a regression against one of the
    documented invariants (FMEntries construction, SSRF precedence,
    content fencing).  Fix the offending site or add a targeted
    ``# nosemgrep: <rule-id>`` suppression with a comment explaining why."""
    assert _SEMGREP_RULES.is_dir(), f"missing rules dir: {_SEMGREP_RULES}"
    result = subprocess.run(
        [
            "semgrep",
            "--config", str(_SEMGREP_RULES),
            "--error",          # non-zero exit on any finding
            "--quiet",          # suppress banner + progress noise
            "--no-git-ignore",  # scan the full tree, not just staged files
            str(_REPO_ROOT / "parkour_mcp"),
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    # semgrep exits 0 on "no findings" and 1 on "findings present"; any
    # other exit code is a tool error we want surfaced distinctly.
    if result.returncode == 0:
        return
    if result.returncode == 1:
        pytest.fail(
            "semgrep rules reported violations:\n\n"
            f"{result.stdout}\n{result.stderr}"
        )
    pytest.fail(
        f"semgrep failed to run (exit {result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
