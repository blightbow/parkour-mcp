"""Verify that Architecture Decision Records in ``docs/adr/`` are append-only.

An ADR freezes when its status first leaves ``proposed``. From that commit
onward, every committed version of the file, and the working tree, must:

- keep the text above the ``append_only_from`` heading unchanged;
- keep the earlier Implementation Notes text as a prefix of the later one;
- change front matter only in ``status`` and ``superseded_by``, along the
  transitions in ``ALLOWED_TRANSITIONS``.

A commit whose message has a line ``ADR-Amend: ADR-NNNN`` may change that
ADR's frozen text. That is the sanctioned route for fixing a typo or a broken
link, and it leaves the change visible in history.

Every ADR, proposed or not, is also checked for well-formed front matter.
The convention these checks enforce is ``docs/adr/README.md``.

Exits 1 and lists each violation. Wired into ``just docs-drift`` and the
docs-drift workflow, and run against this repository by
``tests/test_adr_records.py``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

ADR_DIR = Path("docs/adr")
ADR_FILENAME_RE = re.compile(r"^(\d{4})-[a-z0-9-]+\.md$")
REQUIRED_KEYS = (
    "id", "title", "status", "proposed", "accepted",
    "supersedes", "superseded_by", "append_only_from",
)
STATUSES = ("proposed", "accepted", "rejected", "deprecated", "superseded")
# Status transitions allowed once an ADR has frozen. Staying put is always
# allowed. "proposed" never appears here: leaving it is what freezes a record.
ALLOWED_TRANSITIONS = {
    "accepted": {"deprecated", "superseded"},
    "rejected": set(),
    "deprecated": set(),
    "superseded": set(),
}
MUTABLE_KEYS = {"status", "superseded_by"}


class AdrFormatError(ValueError):
    """An ADR file whose structure cannot be checked."""


@dataclass(frozen=True)
class AdrVersion:
    """One version of an ADR file: a commit, or the working tree."""

    label: str           # short commit hash, or "working tree"
    text: str
    amends: bool         # the commit message carries ADR-Amend for this ADR


@dataclass(frozen=True)
class ParsedAdr:
    front: dict
    frozen: str          # body text up to and including the boundary heading
    notes: str           # everything after the boundary heading


def parse_adr(text: str) -> ParsedAdr:
    """Split an ADR into front matter, frozen body, and notes.

    Raises ``AdrFormatError`` when the file has no front matter block or its
    boundary heading is missing or repeated.
    """
    if not text.startswith("---\n"):
        raise AdrFormatError("no front matter block")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise AdrFormatError("unterminated front matter block")
    front = yaml.safe_load(text[4:end]) or {}
    if not isinstance(front, dict):
        raise AdrFormatError("front matter is not a mapping")
    body = text[end + len("\n---\n"):]
    heading = front.get("append_only_from")
    if not isinstance(heading, str) or not heading.startswith("#"):
        raise AdrFormatError("append_only_from is missing or is not a heading")
    lines = body.split("\n")
    positions = [i for i, line in enumerate(lines) if line.rstrip() == heading]
    if len(positions) != 1:
        raise AdrFormatError(f"heading {heading!r} must appear exactly once, found {len(positions)}")
    cut = positions[0] + 1
    return ParsedAdr(
        front=front,
        frozen="\n".join(lines[:cut]),
        notes="\n".join(lines[cut:]).rstrip(),
    )


def schema_problems(path: Path, parsed: ParsedAdr) -> list[str]:
    """Front-matter problems that apply to every ADR, frozen or not."""
    problems = [f"{path}: front matter is missing {key!r}"
                for key in REQUIRED_KEYS if key not in parsed.front]
    status = parsed.front.get("status")
    if status not in STATUSES:
        problems.append(f"{path}: status {status!r} is not one of {', '.join(STATUSES)}")
    match = ADR_FILENAME_RE.match(path.name)
    expected_id = f"ADR-{match.group(1)}" if match else None
    if parsed.front.get("id") != expected_id:
        problems.append(f"{path}: id {parsed.front.get('id')!r} does not match filename ({expected_id})")
    if (status == "superseded") != bool(parsed.front.get("superseded_by")):
        problems.append(f"{path}: status 'superseded' and superseded_by must be set together")
    return problems


def pair_problems(path: Path, adr_id: str, prev: AdrVersion, cur: AdrVersion) -> list[str]:
    """Violations between two consecutive versions of a frozen ADR."""
    before, after = parse_adr(prev.text), parse_adr(cur.text)
    where = f"{path} ({prev.label} -> {cur.label})"
    problems: list[str] = []
    if before.frozen != after.frozen and not cur.amends:
        problems.append(
            f"{where}: text above {before.front['append_only_from']!r} changed after "
            f"acceptance. Append to Implementation Notes instead, or commit a typo fix "
            f"with an 'ADR-Amend: {adr_id}' line in the message."
        )
    if not after.notes.startswith(before.notes):
        problems.append(f"{where}: an existing Implementation Notes entry was edited or removed; entries are append-only")
    for key in sorted(set(before.front) | set(after.front)):
        if key in MUTABLE_KEYS:
            continue
        if before.front.get(key) != after.front.get(key):
            problems.append(f"{where}: front matter {key!r} changed after acceptance")
    old, new = before.front.get("status"), after.front.get("status")
    if old != new and new not in ALLOWED_TRANSITIONS.get(old, set()):
        problems.append(f"{where}: status may not change from {old!r} to {new!r}")
    return problems


GIT = shutil.which("git") or "git"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed git subcommands, no shell, paths from this repo
        [GIT, *args], cwd=root, check=True, capture_output=True, text=True,
    ).stdout


def _has_commits(root: Path) -> bool:
    return subprocess.run(  # noqa: S603 - fixed git subcommand, no shell
        [GIT, "rev-parse", "--quiet", "--verify", "HEAD"], cwd=root, capture_output=True, check=False,
    ).returncode == 0


def file_versions(root: Path, rel: Path, adr_id: str) -> list[AdrVersion]:
    """Committed versions of ``rel``, oldest first, then the working tree if it differs."""
    amend_re = re.compile(rf"^ADR-Amend:\s*{re.escape(adr_id)}\s*$", re.MULTILINE)
    shas = _git(root, "log", "--format=%H", "--", str(rel)).split() if _has_commits(root) else []
    versions = []
    for sha in reversed(shas):
        message = _git(root, "log", "-1", "--format=%B", sha)
        versions.append(AdrVersion(
            label=sha[:7],
            text=_git(root, "show", f"{sha}:{rel.as_posix()}"),
            amends=bool(amend_re.search(message)),
        ))
    working = (root / rel).read_text()
    if not versions or versions[-1].text != working:
        versions.append(AdrVersion(label="working tree", text=working, amends=False))
    return versions


def check_adr(root: Path, rel: Path) -> list[str]:
    """All violations for one ADR file.

    An ADR is frozen from the first version, committed or in the working tree,
    whose status is not ``proposed``. Frozenness comes from history rather than
    the current status, so setting an accepted ADR back to ``proposed`` does
    not unfreeze it.
    """
    try:
        current = parse_adr((root / rel).read_text())
    except AdrFormatError as exc:
        return [f"{rel}: {exc}"]
    problems = schema_problems(rel, current)
    adr_id = str(current.front.get("id"))
    versions = file_versions(root, rel, adr_id)
    try:
        frozen_at = next((i for i, v in enumerate(versions)
                          if parse_adr(v.text).front.get("status") != "proposed"), None)
    except AdrFormatError as exc:
        return [*problems, f"{rel}: an earlier version does not parse: {exc}"]
    if frozen_at is None:
        return problems
    for prev, cur in zip(versions[frozen_at:], versions[frozen_at + 1:], strict=False):
        try:
            problems += pair_problems(rel, adr_id, prev, cur)
        except AdrFormatError as exc:
            problems.append(f"{rel} ({cur.label}): {exc}")
    return problems


def check_repo(root: Path) -> list[str]:
    """All violations across ``docs/adr/`` in the repository at ``root``."""
    adr_dir = root / ADR_DIR
    if not adr_dir.is_dir():
        return []
    adrs = sorted(p for p in adr_dir.iterdir() if ADR_FILENAME_RE.match(p.name))
    if adrs and _git(root, "rev-parse", "--is-shallow-repository").strip() == "true":
        return ["shallow clone: accepted ADRs are checked against their history; fetch it with 'git fetch --unshallow'"]
    problems: list[str] = []
    for path in adrs:
        problems += check_adr(root, path.relative_to(root))
    return problems


def main() -> int:
    problems = check_repo(Path(__file__).resolve().parent.parent)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(f"{len(problems)} ADR problem(s); see docs/adr/README.md", file=sys.stderr)
        return 1
    print("docs/adr: all ADRs consistent with their accepted versions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
