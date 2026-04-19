"""Tests for scripts/sync_versions.py."""

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent.parent / "scripts" / "sync_versions.py"
_spec = importlib.util.spec_from_file_location("sync_versions", _SCRIPT)
assert _spec is not None and _spec.loader is not None
sync_versions = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync_versions)


class TestPep440ToSemver:
    @pytest.mark.parametrize(
        "pep440,semver",
        [
            ("1.2.0", "1.2.0"),
            ("10.20.30", "10.20.30"),
            ("1.2.0rc1", "1.2.0-rc.1"),
            ("1.2.0rc12", "1.2.0-rc.12"),
            ("1.2.0a0", "1.2.0-alpha.0"),
            ("1.2.0a2", "1.2.0-alpha.2"),
            ("1.2.0b1", "1.2.0-beta.1"),
            ("1.2.0.dev0", "1.2.0-dev.0"),
            ("1.2.0.dev42", "1.2.0-dev.42"),
            ("1.2.0.post1", "1.2.0-post.1"),
        ],
    )
    def test_translates_known_forms(self, pep440: str, semver: str) -> None:
        assert sync_versions.pep440_to_semver(pep440) == semver

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "1.2",
            "1.2.0.0",
            "1.2.0-rc.1",  # SemVer input, not PEP 440
            "v1.2.0",
            "1!1.2.0",  # PEP 440 epoch, intentionally unsupported
            "1.2.0+local",  # PEP 440 local identifier, intentionally unsupported
            "1.2.0rc",  # missing number
            "1.2.0.devabc",
        ],
    )
    def test_rejects_unsupported_forms(self, bad: str) -> None:
        with pytest.raises(ValueError, match="unrecognized PEP 440 version"):
            sync_versions.pep440_to_semver(bad)


def _make_repo(tmp_path: Path, pep440: str, manifest_version: str, server_version: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "test"\nversion = "{pep440}"\n'
    )
    (tmp_path / "manifest.json").write_text(
        json.dumps({"name": "test", "version": manifest_version}, indent=2) + "\n"
    )
    (tmp_path / "server.json").write_text(
        json.dumps({"name": "test", "version": server_version}, indent=2) + "\n"
    )
    return tmp_path


class TestSync:
    def test_final_release_mirrors_verbatim(self, tmp_path: Path) -> None:
        root = _make_repo(tmp_path, "1.2.0", "0.0.0", "0.0.0")
        pep440, semver = sync_versions.sync(root)
        assert pep440 == "1.2.0"
        assert semver == "1.2.0"
        assert sync_versions.read_manifest_version(root) == "1.2.0"
        assert sync_versions.read_server_version(root) == "1.2.0"

    def test_rc_translates_manifest_only(self, tmp_path: Path) -> None:
        root = _make_repo(tmp_path, "1.2.0rc1", "0.0.0", "0.0.0")
        pep440, semver = sync_versions.sync(root)
        assert pep440 == "1.2.0rc1"
        assert semver == "1.2.0-rc.1"
        assert sync_versions.read_manifest_version(root) == "1.2.0-rc.1"
        # server.json keeps PEP 440
        assert sync_versions.read_server_version(root) == "1.2.0rc1"

    def test_preserves_other_manifest_fields(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "1.2.0"\n'
        )
        (tmp_path / "manifest.json").write_text(
            json.dumps(
                {"name": "parkour-mcp", "version": "0.0.0", "description": "keep me"},
                indent=2,
            )
            + "\n"
        )
        (tmp_path / "server.json").write_text(
            json.dumps({"name": "test", "version": "0.0.0", "packages": []}, indent=2) + "\n"
        )
        sync_versions.sync(tmp_path)
        manifest = json.loads((tmp_path / "manifest.json").read_text())
        assert manifest["description"] == "keep me"
        assert manifest["name"] == "parkour-mcp"
        server = json.loads((tmp_path / "server.json").read_text())
        assert server["packages"] == []


class TestCheck:
    def test_passes_when_all_synced(self, tmp_path: Path) -> None:
        root = _make_repo(tmp_path, "1.2.0", "1.2.0", "1.2.0")
        assert sync_versions.check(root) == []

    def test_passes_when_rc_manifest_is_semver(self, tmp_path: Path) -> None:
        root = _make_repo(tmp_path, "1.2.0rc1", "1.2.0-rc.1", "1.2.0rc1")
        assert sync_versions.check(root) == []

    def test_reports_manifest_drift(self, tmp_path: Path) -> None:
        root = _make_repo(tmp_path, "1.2.0rc1", "1.2.0rc1", "1.2.0rc1")
        errors = sync_versions.check(root)
        assert len(errors) == 1
        assert "manifest.json version drift" in errors[0]
        assert "1.2.0-rc.1" in errors[0]

    def test_reports_server_drift(self, tmp_path: Path) -> None:
        root = _make_repo(tmp_path, "1.2.0", "1.2.0", "1.1.9")
        errors = sync_versions.check(root)
        assert len(errors) == 1
        assert "server.json version drift" in errors[0]

    def test_reports_both_drifts(self, tmp_path: Path) -> None:
        root = _make_repo(tmp_path, "1.2.0rc1", "1.2.0rc1", "0.0.0")
        errors = sync_versions.check(root)
        assert len(errors) == 2


class TestMain:
    def test_sync_exits_zero(self, tmp_path: Path) -> None:
        _make_repo(tmp_path, "1.2.0", "0.0.0", "0.0.0")
        assert sync_versions.main(["--root", str(tmp_path)]) == 0

    def test_check_exits_zero_when_synced(self, tmp_path: Path) -> None:
        _make_repo(tmp_path, "1.2.0", "1.2.0", "1.2.0")
        assert sync_versions.main(["--check", "--root", str(tmp_path)]) == 0

    def test_check_exits_nonzero_on_drift(self, tmp_path: Path) -> None:
        _make_repo(tmp_path, "1.2.0", "0.0.0", "0.0.0")
        assert sync_versions.main(["--check", "--root", str(tmp_path)]) == 1
