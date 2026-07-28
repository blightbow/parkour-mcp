"""Run the project's semgrep ruleset as part of pytest.

Rules live in ``.semgrep/`` in the repo root.  Today they enforce:
- FMEntries construction (plain-dict frontmatter entries are banned)
- SSRF precedence (outbound fetch must be preceded by check_url_ssrf)
- Content fencing (no hand-rolled ┌─/└─ markers outside markdown.py)

semgrep is NOT a declared dependency: this module shells out to the
binary and never imports the package, and declaring it pulled semgrep's
hard ``mcp==1.23.3`` pin into our resolve.  Install it out-of-tree with
``brew install semgrep`` or ``uv tool install semgrep``.

Absence is a hard failure, not a skip.  These rules are a security gate
(SSRF precedence, content fencing), and a gate that silently disappears
on a machine that happens to lack the binary is worse than no gate: the
suite goes green and nothing says the checks did not run.  Set
``PARKOUR_SKIP_SEMGREP=1`` to opt out explicitly — that is a decision
someone has to type, which is the point.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest


_REPO_ROOT = pathlib.Path(__file__).parent.parent
_SEMGREP_RULES = _REPO_ROOT / ".semgrep"

_SKIP_ENV = "PARKOUR_SKIP_SEMGREP"


@pytest.mark.skipif(
    os.environ.get(_SKIP_ENV) == "1",
    reason=f"{_SKIP_ENV}=1 set; rules gate explicitly opted out",
)
def test_semgrep_rules_pass():
    """All project-specific semgrep rules produce zero findings on the
    current tree.  A failure here flags a regression against one of the
    documented invariants (FMEntries construction, SSRF precedence,
    content fencing).  Fix the offending site or add a targeted
    ``# nosemgrep: <rule-id>`` suppression with a comment explaining why."""
    assert _SEMGREP_RULES.is_dir(), f"missing rules dir: {_SEMGREP_RULES}"
    semgrep_bin = shutil.which("semgrep")
    if semgrep_bin is None:
        pytest.fail(
            "semgrep binary not found on PATH, so the SSRF-precedence and "
            "content-fence rules did not run.\n\n"
            "It is intentionally not a declared dependency (its hard "
            "`mcp==1.23.3` pin would cap ours). Install it out-of-tree:\n"
            "    brew install semgrep\n"
            "    uv tool install semgrep\n\n"
            f"To run the suite without this gate, set {_SKIP_ENV}=1."
        )
    result = subprocess.run(  # noqa: S603 - fixed args, no shell, trusted dev tool
        [
            semgrep_bin,
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
