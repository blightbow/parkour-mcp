"""Extract a version's section from CHANGELOG.md.

The release workflow feeds the extracted slice to
`gh release create --notes-file` so the GitHub Release body matches the
hand-reviewed CHANGELOG.md prose rather than commit-subject paraphrase.

Usage
-----
    uv run python3 scripts/extract_changelog.py 1.2.0

Writes the matching section to stdout. The heading line (`## [1.2.0] ...`)
is omitted because GitHub renders the release title separately.

Exits non-zero if the version is not present in CHANGELOG.md.
"""

import argparse
import re
import sys
from pathlib import Path


def extract(changelog: str, version: str) -> str:
    """Return the body of `## [version]` through the next `## [` heading.

    The returned text has the heading line stripped, leading and trailing
    blank lines trimmed, and a single trailing newline.
    """
    pattern = re.compile(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(changelog)
    if not match:
        raise LookupError(f"version {version!r} not found in CHANGELOG.md")
    body = match.group(1).strip("\n")
    return body + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract a version's section from CHANGELOG.md."
    )
    parser.add_argument("version", help="version string to extract (no brackets)")
    parser.add_argument(
        "--file",
        type=Path,
        default=Path("CHANGELOG.md"),
        help="path to CHANGELOG.md (default: ./CHANGELOG.md)",
    )
    args = parser.parse_args(argv)

    content = args.file.read_text()
    try:
        body = extract(content, args.version)
    except LookupError as exc:
        print(exc, file=sys.stderr)
        return 1
    sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
