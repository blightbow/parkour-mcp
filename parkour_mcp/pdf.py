"""PDF to markdown for the fetch tools.

Decodes a PDF body into the markdown the section, slice, and search
pipeline consumes, so a PDF fetched by URL gets the same treatment as an
HTML page.  The decoder is ``pdf-inspector`` (pure Rust, MIT); its
markdown output is good on headings, columns, and hyphenation but its
font-size heading tiers admit two kinds of noise that would fabricate
sections, and both are repaired here from the decoder's positioned text
rather than from its markdown:

- A rotated margin run, of which the arXiv stamp is the common case, is
  emitted as a heading in the middle of whatever paragraph it overlaps.
  Rotation is only visible in the positioned items, so those are read to
  learn which lines to drop.
- A display equation or a run of licence boilerplate set in a larger
  face becomes a heading.  Demoted to prose by shape.

Engine choice, licence verdicts, and the measured comparison behind them:
``.claude/TECH_DEBT.md#applicationpdf-support-landed-page-rendering-deferred``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import pdf_inspector

logger = logging.getLogger(__name__)

# arXiv margin stamp as pdf-inspector renders it after whitespace collapse:
# "arXiv:1810.04805v2 [cs.CL] 24 May 2019".  Anchored to a whole line so a
# citation such as "arXiv preprint arXiv:1801.07736" in running text is
# never matched.
_ARXIV_STAMP_RE = re.compile(
    r"^\s*(?:#{1,6}\s+)?arXiv:\d{4}\.\d{4,5}(?:v\d+)?\s+\[[\w.\-]+\]\s+\d{1,2} \w{3} \d{4}\s*$"
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")

# A heading longer than this is a promoted paragraph, not a title.
_MAX_HEADING_CHARS = 120

# A heading carrying any of these is a display equation the tiering
# mistook for a title.
_EQUATION_MARKERS = ("<sub>", "<sup>", " = ")


class PdfDecodeError(Exception):
    """The body could not be decoded as a PDF."""


@dataclass(frozen=True)
class PdfDocument:
    """A decoded PDF: document metadata plus its markdown rendering."""

    title: str | None
    author: str | None
    page_count: int
    markdown: str


def _collapse_ws(text: str) -> str:
    return " ".join(text.split())


def _rotated_run_texts(positioned) -> set[str]:
    """Whitespace-collapsed text of every run rotated relative to its page.

    A page whose text is predominantly rotated (a landscape table, a
    sideways appendix) is listed in ``page_rotations``; its runs are the
    reading text and are kept.  Everything else at a non-zero angle is
    marginalia: stamps, watermarks, spine text.
    """
    turned_pages = {pr.page for pr in positioned.page_rotations}
    return {
        _collapse_ws(item.text)
        for item in positioned.items
        if item.rotation and item.page not in turned_pages and item.text.strip()
    }


def _repair_markdown(markdown: str, rotated: set[str]) -> str:
    """Drop rotated runs and the arXiv stamp; demote paragraphs promoted to headings."""
    out: list[str] = []
    for line in markdown.splitlines():
        if _ARXIV_STAMP_RE.match(line):
            continue
        m = _HEADING_RE.match(line)
        if m is None:
            if rotated and _collapse_ws(line) in rotated:
                continue
            out.append(line)
            continue
        text = m.group(2)
        if rotated and _collapse_ws(text) in rotated:
            continue
        if len(text) > _MAX_HEADING_CHARS or any(mk in text for mk in _EQUATION_MARKERS):
            out.append(text)
            continue
        out.append(line)
    return "\n".join(out).strip() + "\n"


def _first_heading(markdown: str) -> str | None:
    for line in markdown.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            return m.group(2)
    return None


def pdf_to_markdown(data: bytes) -> PdfDocument:
    """Decode *data* into markdown with the noise repairs applied.

    Raises `PdfDecodeError` when the decoder rejects the bytes.  The
    positioned-text pass is best-effort: if it fails, the markdown is
    returned with the stamp regex as the only rotation defence.
    """
    try:
        result = pdf_inspector.process_pdf_bytes(data)
    except Exception as e:
        raise PdfDecodeError(str(e)) from e

    rotated: set[str] = set()
    try:
        rotated = _rotated_run_texts(
            pdf_inspector.extract_text_with_positions_and_rotations_bytes(data)
        )
    except Exception:
        logger.debug("pdf-inspector positioned-text pass failed; stamp regex only", exc_info=True)

    markdown = _repair_markdown(result.markdown or "", rotated)
    title = (result.title or "").strip() or _first_heading(markdown)
    author = (result.author or "").strip() or None
    return PdfDocument(
        title=title,
        author=author,
        page_count=int(result.page_count or 0),
        markdown=markdown,
    )
