"""Guards for the pure-detection module.

The detector behaviour itself is covered by the per-source suites
(test_arxiv, test_doi, test_semantic_scholar, test_ietf, test_reddit), which
import the functions from their canonical home here.  This module guards the
one property those suites do not: that detection.py stays cheap to import, so
the fast-path dispatchers and sibling tools can depend on it without dragging
in a source module's transport stack (httpx, curl_cffi, tree-sitter, yt-dlp).
"""

import ast
from pathlib import Path

import parkour_mcp.detection as detection

_DETECTION_SRC = Path(detection.__file__)

# Third-party / native packages that must never become an import-time
# dependency of detection.py.  Importing any of these would defeat the reason
# the module exists.
_FORBIDDEN_ROOTS = {
    "httpx",
    "curl_cffi",
    "tree_sitter",
    "tree_sitter_language_pack",
    "yt_dlp",
    "playwright",
    "tantivy",
    "semantic_text_splitter",
    "bs4",
    "markdownify",
    "htmd",
    "pydantic",
}


def _module_level_imports(source: str) -> set[str]:
    """Return the top-level (non-deferred) imported root module names."""
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in tree.body:  # module body only — ignore imports inside functions
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # Relative import (from . / from .x) has level > 0.
            if node.level:
                roots.add("." * node.level + (node.module or ""))
            elif node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_detection_imports_are_stdlib_only():
    roots = _module_level_imports(_DETECTION_SRC.read_text())

    # No heavy third-party dependency.
    assert not (roots & _FORBIDDEN_ROOTS), (
        f"detection.py must stay dependency-free; found {roots & _FORBIDDEN_ROOTS}"
    )

    # No coupling back into the package — a relative import would let a heavy
    # source module sneak in transitively and recreate the cycle we removed.
    relative = {r for r in roots if r.startswith(".") or r == "parkour_mcp"}
    assert not relative, f"detection.py must not import from the package; found {relative}"
