"""Check pyproject.toml's dependency declarations against the code with deptry.

deptry compares every import in ``parkour_mcp/`` with the declared
dependencies and fails on four kinds of gap: an import with no declaration
(DEP001), a declaration nothing imports (DEP002), an import satisfied only
because some other dependency happens to pull the package in (DEP003), and a
dev-only dependency imported by shipped code (DEP004). DEP003 matters most
here: such a package can disappear from a fresh PyPI install whenever the
dependency that brings it changes its own requirements, and the lockfile
hides that locally. Exceptions live in ``[tool.deptry]`` in pyproject.toml,
each with its reason.

deptry is a dev dependency and runs as ``python -m deptry``, so unlike the
semgrep gate there is no separate binary that can be missing.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

_REPO_ROOT = pathlib.Path(__file__).parent.parent


def _deptry(root: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "deptry", ".", "--no-ansi"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_declared_dependencies_match_imports():
    """Every package parkour_mcp imports is declared, and every declaration is used."""
    result = _deptry(_REPO_ROOT)
    assert result.returncode == 0, (
        "deptry found dependency issues. Declare the package in "
        "pyproject.toml, or, if the finding is a deliberate exception, add it "
        "to [tool.deptry] with the reason.\n\n" + result.stdout + result.stderr
    )


def test_gate_reports_an_undeclared_transitive_import(tmp_path):
    """Removing a direct declaration makes deptry fail with DEP003.

    pydantic is imported throughout the package and was once present only
    because mcp requires it; dropping its declaration recreates that state.
    """
    shutil.copytree(_REPO_ROOT / "parkour_mcp", tmp_path / "parkour_mcp")
    pyproject = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lines = [line for line in pyproject.splitlines() if '"pydantic>=' not in line]
    assert len(lines) == len(pyproject.splitlines()) - 1, "pydantic declaration not found"
    (tmp_path / "pyproject.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = _deptry(tmp_path)

    assert result.returncode != 0
    assert "DEP003 'pydantic'" in result.stdout + result.stderr
