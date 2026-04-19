"""Sync version across pyproject.toml, manifest.json, and server.json.

`pyproject.toml:project.version` is the single source of truth (PEP 440).
python-semantic-release writes it directly during a release bump; this
script mirrors the value out to the sibling files that track it.

`manifest.json` is consumed by Claude Desktop, which rejects PEP 440
pre-release forms like `1.2.0rc1`. The PEP 440 string is translated to
strict SemVer 2.0 (`1.2.0-rc.1`) before writing.

`server.json` is consumed by the MCP Registry and accepts PEP 440
verbatim.

Usage
-----
    uv run python3 scripts/sync_versions.py
        Read pyproject.toml, write translated manifest and server versions.

    uv run python3 scripts/sync_versions.py --check
        Exit non-zero if the three files disagree. Used by `just tag` as
        a pre-push guard and by the release workflow's tag-version check.
"""

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

_PEP440_RE = re.compile(
    r"""
    ^
    (?P<base>\d+\.\d+\.\d+)
    (?:
        (?P<pre_kind>a|b|rc)(?P<pre_num>\d+)
      | \.(?P<post_or_dev>dev|post)(?P<post_or_dev_num>\d+)
    )?
    $
    """,
    re.VERBOSE,
)

_PEP440_KIND_TO_SEMVER = {
    "a": "alpha",
    "b": "beta",
    "rc": "rc",
    "dev": "dev",
    "post": "post",
}


def pep440_to_semver(version: str) -> str:
    """Translate a PEP 440 version to strict SemVer 2.0.

    Final releases pass through unchanged (`1.2.0` is valid in both).
    Pre-release and dev/post identifiers gain a dash and a dot:

        1.2.0rc1    -> 1.2.0-rc.1
        1.2.0a2     -> 1.2.0-alpha.2
        1.2.0b1     -> 1.2.0-beta.1
        1.2.0.dev0  -> 1.2.0-dev.0
        1.2.0.post1 -> 1.2.0-post.1

    Raises ValueError on unrecognized input. The regex is intentionally
    narrow: parkour-mcp only releases X.Y.Z[kindN] forms, so epoch
    versions (`1!1.0`) and local identifiers (`1.0+local`) are rejected
    rather than silently mistranslated.
    """
    match = _PEP440_RE.match(version)
    if not match:
        raise ValueError(f"unrecognized PEP 440 version: {version!r}")
    base = match.group("base")
    kind = match.group("pre_kind") or match.group("post_or_dev")
    num = match.group("pre_num") or match.group("post_or_dev_num")
    if kind is None:
        return base
    return f"{base}-{_PEP440_KIND_TO_SEMVER[kind]}.{num}"


def read_pyproject_version(root: Path) -> str:
    data = tomllib.loads((root / "pyproject.toml").read_text())
    return data["project"]["version"]


def read_manifest_version(root: Path) -> str:
    return json.loads((root / "manifest.json").read_text())["version"]


def read_server_version(root: Path) -> str:
    return json.loads((root / "server.json").read_text())["version"]


_VERSION_LINE_RE = re.compile(r'("version"\s*:\s*)"[^"]*"')


def _rewrite_version_line(path: Path, new_version: str) -> None:
    """Replace the first `"version": "..."` occurrence in a JSON file.

    Uses line-level regex rather than json.dumps round-tripping so existing
    formatting survives: no reflow of compact arrays, no \\u escaping of
    non-ASCII characters, no key reordering. manifest.json and server.json
    both have a single top-level `version` key, so the first match is the
    right one.
    """
    content = path.read_text()
    new_content, count = _VERSION_LINE_RE.subn(
        lambda m: f'{m.group(1)}"{new_version}"', content, count=1
    )
    if count == 0:
        raise RuntimeError(f"no \"version\" key found in {path}")
    path.write_text(new_content)


def write_manifest_version(root: Path, semver: str) -> None:
    _rewrite_version_line(root / "manifest.json", semver)


def write_server_version(root: Path, pep440: str) -> None:
    _rewrite_version_line(root / "server.json", pep440)


def sync(root: Path) -> tuple[str, str]:
    pep440 = read_pyproject_version(root)
    semver = pep440_to_semver(pep440)
    write_manifest_version(root, semver)
    write_server_version(root, pep440)
    return pep440, semver


def check(root: Path) -> list[str]:
    """Return a list of human-readable drift messages. Empty list means in sync."""
    pep440 = read_pyproject_version(root)
    expected_semver = pep440_to_semver(pep440)
    errors: list[str] = []
    manifest_actual = read_manifest_version(root)
    if manifest_actual != expected_semver:
        errors.append(
            f"manifest.json version drift: expected {expected_semver!r} "
            f"(from pyproject {pep440!r}), found {manifest_actual!r}"
        )
    server_actual = read_server_version(root)
    if server_actual != pep440:
        errors.append(
            f"server.json version drift: expected {pep440!r}, "
            f"found {server_actual!r}"
        )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sync version across pyproject.toml, manifest.json, and server.json."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify manifest.json and server.json agree with pyproject.toml, "
             "exit non-zero on drift (no writes)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repo root (default: cwd)",
    )
    args = parser.parse_args(argv)

    if args.check:
        errors = check(args.root)
        if errors:
            for message in errors:
                print(message, file=sys.stderr)
            return 1
        print("version files in sync")
        return 0

    pep440, semver = sync(args.root)
    print(f"pyproject.toml: {pep440}")
    print(f"manifest.json:  {semver}")
    print(f"server.json:    {pep440}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
