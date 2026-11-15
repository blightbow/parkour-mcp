"""Verify that ``path#fragment`` code citations in the docs still resolve.

The project cites code as ``path#Symbol`` (see CLAUDE.md, "Code
references"). drift checks only the anchors bound in ``drift.lock`` and
the markdown links in a doc; a backticked citation is plain text to it, so
a renamed or deleted symbol leaves the citation pointing at nothing. This
script resolves every such citation:

- ``file.py#Name`` must name a class, function, or assignment in that
  file. ``#Class.member`` walks into the class body. A bare name also
  matches a definition nested inside a function, such as a handler
  registered inside ``main()``.
- ``doc.md#slug`` must match a heading in that doc, slugged the way
  GitHub renders heading anchors.

A citation is a code span that consists of nothing but the citation, so
prose that merely mentions a name is never parsed as one. A path that
does not exist is resolved against ``parkour_mcp/`` as well, since
shorthand like ``common.py#guarded_fetch`` is the house style; a path that
matches nothing in either place is treated as a citation of another
project's code and skipped.

Exits 1 and lists each broken citation by doc and line. Wired into
``just docs-drift``.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "parkour_mcp"

# Docs whose citations are expected to resolve. CHANGELOG.md is excluded
# because it records history, and a release note citing a symbol that was
# later removed is accurate. Vendored skills cite their upstream's code.
DOC_GLOBS = ("CLAUDE.md", "README.md", "docs/**/*.md", ".claude/*.md")

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_SPAN_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
_CITATION_RE = re.compile(
    r"^(?P<path>[\w./-]+\.(?:py|md))#(?P<fragment>[\w.-]+)$"
)


def iter_docs() -> Iterator[Path]:
    seen: set[Path] = set()
    for pattern in DOC_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            if path.is_file() and path not in seen:
                seen.add(path)
                yield path


def iter_citations(text: str) -> Iterator[tuple[int, str, str]]:
    """Yield ``(line_number, path, fragment)`` for each citation outside fences."""
    in_fence = False
    for number, line in enumerate(text.splitlines(), start=1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for span in _SPAN_RE.findall(line):
            if match := _CITATION_RE.match(span.strip()):
                yield number, match["path"], match["fragment"]


def resolve_path(cited: str, doc: Path) -> Path | None:
    """Return the cited file, or None when it belongs to another project."""
    for base in (ROOT, doc.parent, PACKAGE):
        candidate = (base / cited).resolve()
        if candidate.is_file():
            return candidate
    return None


def _scope_names(body: list[ast.stmt]) -> dict[str, ast.AST]:
    names: dict[str, ast.AST] = {}
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names[target.id] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names[node.target.id] = node
    return names


def python_symbol_exists(source: str, fragment: str) -> bool:
    tree = ast.parse(source)
    head, *rest = fragment.split(".")
    node = _scope_names(tree.body).get(head)
    if node is None and not rest:
        return any(
            isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and n.name == head
            for n in ast.walk(tree)
        )
    for part in rest:
        if not isinstance(node, ast.ClassDef):
            return False
        node = _scope_names(node.body).get(part)
    return node is not None


def github_slug(heading: str) -> str:
    """Slug a heading the way GitHub builds its anchor."""
    text = re.sub(r"`([^`]*)`", r"\1", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def markdown_slugs(text: str) -> set[str]:
    slugs: set[str] = set()
    counts: dict[str, int] = {}
    in_fence = False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or not (match := re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)):
            continue
        slug = github_slug(match[1])
        seen = counts.get(slug, 0)
        counts[slug] = seen + 1
        slugs.add(slug if seen == 0 else f"{slug}-{seen}")
    return slugs


def check_citation(cited: str, fragment: str, doc: Path) -> str | None:
    """Return why the citation is broken, or None when it resolves or is external."""
    target = resolve_path(cited, doc)
    if target is None:
        return None
    text = target.read_text(encoding="utf-8")
    if target.suffix == ".py":
        if not python_symbol_exists(text, fragment):
            return f"no symbol {fragment!r} in {target.relative_to(ROOT)}"
    elif fragment not in markdown_slugs(text):
        return f"no heading slug {fragment!r} in {target.relative_to(ROOT)}"
    return None


def main() -> int:
    failures = []
    checked = 0
    for doc in iter_docs():
        for number, cited, fragment in iter_citations(doc.read_text(encoding="utf-8")):
            checked += 1
            if reason := check_citation(cited, fragment, doc):
                failures.append(f"{doc.relative_to(ROOT)}:{number}: `{cited}#{fragment}`: {reason}")
    if failures:
        print("\n".join(failures))
        print(f"{len(failures)} of {checked} citations do not resolve.")
        return 1
    print(f"doc citations: {checked} resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
