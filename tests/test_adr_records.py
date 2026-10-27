"""Tests for scripts/check_adr_records.py."""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent.parent / "scripts" / "check_adr_records.py"
_spec = importlib.util.spec_from_file_location("check_adr_records", _SCRIPT)
assert _spec is not None and _spec.loader is not None
check_adr_records = importlib.util.module_from_spec(_spec)
# dataclasses resolves the module's string annotations through sys.modules.
sys.modules[_spec.name] = check_adr_records
_spec.loader.exec_module(check_adr_records)

ADR_PATH = Path("docs/adr/0001-example-decision.md")
GIT = shutil.which("git") or "git"
FROZEN_BODY = "# ADR-0001: Example decision\n\n## Decision\n\nUse the thing.\n"


def _adr(
    *,
    status: str = "proposed",
    adr_id: str = "ADR-0001",
    title: str = "Example decision",
    superseded_by: str = "null",
    body: str = FROZEN_BODY,
    notes: str = "",
    heading: str = "## Implementation Notes",
) -> str:
    return (
        "---\n"
        f"id: {adr_id}\n"
        f"title: {title}\n"
        f"status: {status}\n"
        "proposed: 2026-10-26\n"
        "accepted: null\n"
        "supersedes: []\n"
        f"superseded_by: {superseded_by}\n"
        'append_only_from: "## Implementation Notes"\n'
        "---\n\n"
        f"{body}\n"
        f"{heading}\n"
        f"{notes}"
    )


class AdrRepo:
    """A throwaway git repository holding one ADR."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "Test")
        self._git("config", "commit.gpgsign", "false")

    def _git(self, *args: str) -> None:
        subprocess.run(  # noqa: S603 - fixed git subcommands against a tmp_path repo, no shell
            [GIT, *args], cwd=self.root, check=True, capture_output=True,
        )

    def write(self, text: str) -> None:
        path = self.root / ADR_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, text: str, message: str = "docs: update ADR") -> None:
        self.write(text)
        self._git("add", str(ADR_PATH))
        self._git("commit", "-q", "-m", message)

    def problems(self) -> list[str]:
        return check_adr_records.check_repo(self.root)


@pytest.fixture
def repo(tmp_path: Path) -> AdrRepo:
    return AdrRepo(tmp_path)


@pytest.fixture
def accepted_repo(repo: AdrRepo) -> AdrRepo:
    """An ADR proposed in one commit and accepted in the next."""
    repo.commit(_adr())
    repo.commit(_adr(status="accepted"), "docs(adr): accept ADR-0001")
    return repo


class TestProposed:
    def test_proposed_adr_may_change_freely(self, repo: AdrRepo) -> None:
        repo.commit(_adr())
        repo.commit(_adr(body="# ADR-0001: Example decision\n\n## Decision\n\nUse another thing.\n"))
        assert repo.problems() == []

    def test_acceptance_in_working_tree_has_nothing_to_compare(self, repo: AdrRepo) -> None:
        repo.commit(_adr())
        repo.write(_adr(status="accepted"))
        assert repo.problems() == []


class TestFrozenText:
    def test_unchanged_accepted_adr_passes(self, accepted_repo: AdrRepo) -> None:
        assert accepted_repo.problems() == []

    def test_frozen_edit_in_working_tree_fails(self, accepted_repo: AdrRepo) -> None:
        accepted_repo.write(_adr(status="accepted", body=FROZEN_BODY.replace("the thing", "a better thing")))
        problems = accepted_repo.problems()
        assert len(problems) == 1
        assert "changed after acceptance" in problems[0]
        assert "ADR-Amend: ADR-0001" in problems[0]

    def test_frozen_edit_in_later_commit_fails(self, accepted_repo: AdrRepo) -> None:
        accepted_repo.commit(_adr(status="accepted", body=FROZEN_BODY.replace("the thing", "a better thing")))
        assert any("changed after acceptance" in p for p in accepted_repo.problems())

    def test_amend_commit_may_change_frozen_text(self, accepted_repo: AdrRepo) -> None:
        accepted_repo.commit(
            _adr(status="accepted", body=FROZEN_BODY.replace("Use the thing", "Use the thing.")),
            "docs(adr): fix a typo in ADR-0001\n\nADR-Amend: ADR-0001",
        )
        assert accepted_repo.problems() == []

    def test_amend_for_another_adr_does_not_apply(self, accepted_repo: AdrRepo) -> None:
        accepted_repo.commit(
            _adr(status="accepted", body=FROZEN_BODY.replace("the thing", "a better thing")),
            "docs(adr): reword\n\nADR-Amend: ADR-0002",
        )
        assert any("changed after acceptance" in p for p in accepted_repo.problems())

    def test_front_matter_title_is_frozen(self, accepted_repo: AdrRepo) -> None:
        accepted_repo.write(_adr(status="accepted", title="Renamed decision"))
        assert any("'title' changed after acceptance" in p for p in accepted_repo.problems())


class TestImplementationNotes:
    ENTRY = "\n### 2026-11-01 — Route moved (Deviation)\n\n- **Instead:** abc1234\n"

    def test_appending_an_entry_passes(self, accepted_repo: AdrRepo) -> None:
        accepted_repo.commit(_adr(status="accepted", notes=self.ENTRY))
        accepted_repo.write(_adr(status="accepted", notes=self.ENTRY + "\n### 2026-12-01 — v2.3.0 (Shipped)\n"))
        assert accepted_repo.problems() == []

    def test_editing_an_existing_entry_fails(self, accepted_repo: AdrRepo) -> None:
        accepted_repo.commit(_adr(status="accepted", notes=self.ENTRY))
        accepted_repo.write(_adr(status="accepted", notes=self.ENTRY.replace("abc1234", "def5678")))
        assert any("append-only" in p for p in accepted_repo.problems())

    def test_removing_an_entry_fails(self, accepted_repo: AdrRepo) -> None:
        accepted_repo.commit(_adr(status="accepted", notes=self.ENTRY))
        accepted_repo.write(_adr(status="accepted"))
        assert any("append-only" in p for p in accepted_repo.problems())


class TestStatusTransitions:
    def test_superseding_passes(self, accepted_repo: AdrRepo) -> None:
        accepted_repo.write(_adr(status="superseded", superseded_by="ADR-0002"))
        assert accepted_repo.problems() == []

    def test_deprecating_passes(self, accepted_repo: AdrRepo) -> None:
        accepted_repo.write(_adr(status="deprecated"))
        assert accepted_repo.problems() == []

    def test_returning_to_proposed_fails(self, accepted_repo: AdrRepo) -> None:
        accepted_repo.write(_adr(status="proposed"))
        assert any("from 'accepted' to 'proposed'" in p for p in accepted_repo.problems())

    def test_rejected_is_final(self, repo: AdrRepo) -> None:
        repo.commit(_adr())
        repo.commit(_adr(status="rejected"))
        repo.write(_adr(status="accepted"))
        assert any("from 'rejected' to 'accepted'" in p for p in repo.problems())


class TestSchema:
    @pytest.mark.parametrize("status", ["implemented", "abandoned", "Accepted"])
    def test_status_outside_madr_vocabulary_fails(self, repo: AdrRepo, status: str) -> None:
        repo.write(_adr(status=status))
        assert any("is not one of" in p for p in repo.problems())

    def test_id_must_match_filename(self, repo: AdrRepo) -> None:
        repo.write(_adr(adr_id="ADR-0007"))
        assert any("does not match filename" in p for p in repo.problems())

    def test_missing_boundary_heading_fails(self, repo: AdrRepo) -> None:
        repo.write(_adr(heading="## Notes"))
        assert any("must appear exactly once" in p for p in repo.problems())

    def test_superseded_requires_superseded_by(self, repo: AdrRepo) -> None:
        repo.write(_adr(status="superseded"))
        assert any("must be set together" in p for p in repo.problems())

    def test_readme_is_not_an_adr(self, repo: AdrRepo) -> None:
        (repo.root / "docs/adr").mkdir(parents=True)
        (repo.root / "docs/adr/README.md").write_text("# Architecture Decision Records\n")
        assert repo.problems() == []


def test_shallow_clone_fails_closed(accepted_repo: AdrRepo, tmp_path: Path) -> None:
    clone = tmp_path / "shallow"
    subprocess.run(  # noqa: S603 - fixed git clone of a tmp_path repo, no shell
        [GIT, "clone", "-q", "--depth", "1", f"file://{accepted_repo.root}", str(clone)],
        check=True, capture_output=True,
    )
    problems = check_adr_records.check_repo(clone)
    assert len(problems) == 1
    assert "shallow clone" in problems[0]


def test_repository_adrs_are_consistent() -> None:
    """The live gate: this repository's own ADRs against their history."""
    root = Path(__file__).resolve().parent.parent
    assert check_adr_records.check_repo(root) == []
