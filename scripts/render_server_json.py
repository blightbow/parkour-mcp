"""Update release-specific fields in server.json.

At release time (in CI, after the mcpb bundle is built), this script
rewrites the `packages[0].identifier` download URL and `fileSha256`
digest so the MCP Registry manifest points at the just-uploaded asset.

The `version` field is NOT touched here. It's kept in sync with
pyproject.toml by scripts/sync_versions.py and is already correct on
the release commit.

Line-level regex edits are used (same approach as sync_versions.py) so
existing formatting survives: no reflow of compact arrays, no
\\u-escaping of non-ASCII characters, no key reordering.

Usage
-----
    uv run python3 scripts/render_server_json.py \\
        --identifier "https://github.com/owner/repo/releases/download/vX.Y.Z/artifact.mcpb" \\
        --sha256 "<64-hex>"
"""

import argparse
import re
import sys
from pathlib import Path

_IDENTIFIER_RE = re.compile(r'("identifier"\s*:\s*)"[^"]*"')
_SHA256_RE = re.compile(r'("fileSha256"\s*:\s*)"[^"]*"')
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def rewrite(content: str, identifier: str, sha256: str) -> str:
    new_content, id_count = _IDENTIFIER_RE.subn(
        lambda m: f'{m.group(1)}"{identifier}"', content, count=1
    )
    new_content, sha_count = _SHA256_RE.subn(
        lambda m: f'{m.group(1)}"{sha256}"', new_content, count=1
    )
    if id_count == 0:
        raise RuntimeError('no "identifier" key found in server.json')
    if sha_count == 0:
        raise RuntimeError('no "fileSha256" key found in server.json')
    return new_content


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Update server.json identifier URL and fileSha256."
    )
    parser.add_argument("--identifier", required=True, help="mcpb asset download URL")
    parser.add_argument("--sha256", required=True, help="mcpb asset sha256 digest (64 hex chars)")
    parser.add_argument(
        "--file",
        type=Path,
        default=Path("server.json"),
        help="path to server.json (default: ./server.json)",
    )
    args = parser.parse_args(argv)

    if not _SHA256_HEX_RE.match(args.sha256):
        print(f"expected 64-char lowercase hex sha256, got {args.sha256!r}", file=sys.stderr)
        return 1

    content = args.file.read_text()
    args.file.write_text(rewrite(content, args.identifier, args.sha256))
    print(f"identifier: {args.identifier}")
    print(f"sha256:     {args.sha256}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
