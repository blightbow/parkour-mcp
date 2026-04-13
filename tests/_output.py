"""Helpers for asserting on tool output shape.

Keeps knowledge of the frontmatter / content-fence format in one place so
mocked and live tests stay in sync when production changes the output shape.

Tool output has two zones with different trust properties::

    ---                          <-- frontmatter (trusted, server-generated)
    source: https://...
    trust: ...
    ---

    ┌─ untrusted content          <-- fence (untrusted, from external source)
    │
    │ # Page Title
    │ ...body...
    │
    └─ untrusted content

Attacker-controlled data (page titles, section headings, matched fragments,
heading names) must live inside the fence. Tests that care about the
trust boundary should use :func:`split_output` to assert that invariant
explicitly rather than searching the whole result string.
"""

from parkour_mcp.markdown import (
    _FENCE_CLOSE,
    _FENCE_OPEN,
    _TRUST_ADVISORY,
)

FENCE_OPEN = _FENCE_OPEN
FENCE_CLOSE = _FENCE_CLOSE
TRUST_ADVISORY = _TRUST_ADVISORY
FENCE_LINE_PREFIX = "│ "


def split_output(result: str) -> tuple[str, str]:
    """Split a tool result into ``(frontmatter, fenced_content)``.

    Frontmatter is the trusted zone — server-generated metadata between the
    ``---`` markers. Fenced content is everything after, wrapped in ┌─/└─
    markers with each line prefixed by ``│``.

    Returns the two regions with surrounding blank lines trimmed. Raises
    ``AssertionError`` if the expected structure is absent so callers can
    rely on the split having worked before inspecting the pieces.
    """
    assert result.startswith("---\n"), (
        f"result missing frontmatter opener; got: {result[:80]!r}"
    )
    parts = result.split("---", 2)
    assert len(parts) == 3, (
        f"result does not contain a closing --- marker; "
        f"got {len(parts) - 1} marker(s)"
    )
    frontmatter = parts[1].strip("\n")
    fenced = parts[2].lstrip("\n")
    return frontmatter, fenced


def fenced_heading(level: int, text: str) -> str:
    """Return the markdown heading line as it appears inside a fence.

    Inside a fence every line is prefixed with ``│ ``. A markdown heading at
    level N appears as ``│ {N * '#'} {text}``. Use the return value as an
    ``in result`` substring::

        assert fenced_heading(1, "Ultima VIII books") in result
        assert fenced_heading(2, "Honor Lost") in result
    """
    assert 1 <= level <= 6, f"heading level must be 1..6, got {level}"
    return f"{FENCE_LINE_PREFIX}{'#' * level} {text}"


def fenced_line(text: str) -> str:
    """Return a plain line as it appears inside a fence (``│ `` prefix)."""
    return f"{FENCE_LINE_PREFIX}{text}"


def assert_fenced(result: str) -> None:
    """Assert that result contains both fence markers."""
    assert FENCE_OPEN in result, "result is missing the fence open marker"
    assert FENCE_CLOSE in result, "result is missing the fence close marker"
