"""HTML-to-markdown conversion and section extraction helpers.

The frontmatter machinery (entries container, ``---`` builder, content
fence, tip ledger) lives in the ``betamatter`` library, extracted from
this module.  This module keeps thin parkour-side bindings — the
``sections_not_found`` special case, the ``_TIPS`` registry wired to the
profile-aware ``tool_name`` resolver, and the underscore-prefixed names
the rest of the package imports — so call sites are unchanged.
"""

import re
from collections.abc import Mapping
from typing import NotRequired, TypedDict
from urllib.parse import unquote

import htmd
from betamatter import (
    FENCE_CLOSE,
    FENCE_OPEN,
    TRUST_ADVISORY,
    TipLedger,
    append_frontmatter_entry,
    build_frontmatter,
    fence_content,
    sanitize_label,
)
from betamatter import FMEntries as _BetamatterFMEntries
from bs4 import BeautifulSoup
from markdownify import MarkdownConverter
from semantic_text_splitter import MarkdownSplitter

from .common import tool_name


class TextOnlyConverter(MarkdownConverter):
    """Custom converter that preserves link hrefs but strips non-text content like images.

    Retained for ``_mediawiki_html_to_markdown`` which applies MediaWiki-specific
    BS4 transforms (navbox pruning, math extraction, citation footnote rewriting)
    before conversion. The generic HTML path in ``html_to_markdown()`` uses the
    Rust-backed ``htmd`` library directly.
    """

    def convert_img(self, el, text, parent_tags):
        del text, parent_tags  # required by override signature
        # Images can't render as text - return alt text only if meaningful
        alt = el.get('alt', '').strip()
        return f'[Image: {alt}]' if alt else ''

    def convert_a(self, el, text, parent_tags):
        # If the link only contains an image reference, drop it entirely
        stripped = text.strip()
        if stripped.startswith('[Image:') or not stripped:
            return stripped
        # Otherwise use default link conversion
        return super().convert_a(el, text, parent_tags)  # ty: ignore[unresolved-attribute]


def md(html, **options):
    """Convert HTML to markdown using custom converter."""
    return TextOnlyConverter(**options).convert(html)


# Noise tags dropped entirely during generic-HTML conversion.
# The Rust-backed path uses this as a visitor-level skip set; the MediaWiki
# path still uses it as a BS4 decompose list via ``_mediawiki_html_to_markdown``.
_NOISE_TAGS = ["script", "style", "nav", "header", "footer", "aside", "noscript"]
_NOISE_TAGS_SET = frozenset(_NOISE_TAGS)

# SPA framework root container IDs — empty containers indicate JS-rendered content
_SPA_ROOT_IDS = ("root", "app", "__next", "__nuxt", "__svelte", "__gatsby")


def _detect_js_dependent(html: str) -> bool:
    """Detect if an HTML page likely requires JavaScript to render content.

    Only called when html_to_markdown() returned empty/minimal content, so the
    bar for a positive signal is low — we just need to distinguish JS-dependent
    pages from genuinely empty or broken HTML.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Signal 1: <noscript> tag mentioning JavaScript
    for ns in soup.find_all("noscript"):
        if "javascript" in ns.get_text().lower():
            return True

    # Signal 2: Known SPA framework root containers that are empty
    for spa_id in _SPA_ROOT_IDS:
        el = soup.find(id=spa_id)
        if el and not el.get_text(strip=True):
            return True

    return False


_HEADING_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6"]


def _clean_headings(soup: BeautifulSoup) -> None:
    """Simplify heading elements so markdown section names are clean.

    Unwraps inline markup (links, bold, italic) and removes edit-section
    spans, so markdownify produces plain-text headings.
    """
    for heading in soup.find_all(_HEADING_TAGS):
        # Remove MediaWiki edit-section links
        for edit in heading.select(".mw-editsection"):
            edit.decompose()
        # Unwrap inline markup — replaces tag with its children
        for tag in heading.find_all(["a", "b", "strong", "i", "em", "span"]):
            tag.unwrap()


# Regex matchers for stripping inline markdown formatting from heading text.
# ``htmd`` preserves inline children inside headings (e.g. ``<h1>Real
# <strong>Heading</strong></h1>`` renders as ``# Real **Heading**``), whereas
# the downstream section-extraction logic expects plain text.
_HEADING_MD_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_HEADING_MD_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_HEADING_MD_CODE = re.compile(r"`([^`]+)`")
# ``[^\]]*`` (not ``+``) so empty-text permalink anchors such as
# ``[](#introduction)`` — emitted by spec documents like the WHATWG HTML
# Living Standard that render self-link ``<a>`` elements with no child
# text — are stripped rather than left inline.  Otherwise the captured
# section name includes the anchor syntax and section-by-name matching
# in ``_filter_markdown_by_sections`` fails because callers type the
# human-visible heading text, not the permalink.
#
# URL portion uses ``(?:\\[()]|[^()])*`` instead of ``[^)]*`` so escaped
# parens (``\(`` and ``\)``) are honored as literals rather than closing
# the URL.  htmd correctly escapes parens per CommonMark when the
# source ``<a href>`` contains them — WHATWG's self-links like
# ``href="#attribute-value-(double-quoted)-state"`` come out as
# ``[](#attribute-value-\(double-quoted\)-state)``.  The naive
# ``[^)]*`` regex stopped at the first ``\)``, leaving ``-state)`` in
# the heading text and producing doubled segments in derived slugs
# (e.g. ``#attribute-value-double-quoted-state-state``).
_HEADING_MD_LINK = re.compile(r"\[([^\]]*)\]\((?:\\[()]|[^()])*\)")
_HEADING_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\((?:\\[()]|[^()])*\)")


def _strip_heading_markdown(text: str) -> str:
    """Recover plain text from rendered-markdown heading text.

    Undoes the common inline transforms so heading labels match what the
    pre-port ``_clean_headings`` + ``get_text(strip=True)`` path produced.
    Strips images before links, links before emphasis, bold before italic
    to avoid partial overlaps.
    """
    text = _HEADING_MD_IMAGE.sub(r"\1", text)
    text = _HEADING_MD_LINK.sub(r"\1", text)
    text = _HEADING_MD_BOLD.sub(r"\1", text)
    text = _HEADING_MD_ITALIC.sub(r"\1", text)
    text = _HEADING_MD_CODE.sub(r"\1", text)
    return text.strip()


# Matches a markdown heading line (``#`` through ``######``) with trailing
# whitespace trimmed. Used by ``html_to_markdown()`` to strip inline markup
# from heading lines once at conversion time, so the cached markdown matches
# the pre-port ``_clean_headings`` output format and section-name extraction
# stays on its pre-port fast path.
_HEADING_LINE_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)

# Matches the first level-1 heading line in cleaned markdown output. Used to
# recover the h1-first title behavior without re-parsing the source HTML.
_FIRST_H1_RE = re.compile(r"^# (.+)$", re.MULTILINE)


def _strip_heading_line(match: "re.Match[str]") -> str:
    return f"{match.group(1)} {_strip_heading_markdown(match.group(2))}"


# Built once; ``htmd.Options`` is an immutable configuration snapshot that
# replicates the prior ``TextOnlyConverter`` semantics via flat fields
# (``skip_tags`` decomposes noise, ``image_placeholder`` renders images as
# ``[Image: alt]``, ``drop_empty_alt_images`` drops images without alt text,
# ``drop_image_only_links`` strips ``<a>`` wrappers whose only child is an
# image).
#
# Noise tags used by the generic path additionally include ``head`` to
# suppress the ``<title>`` + ``<meta>`` text that would otherwise leak into
# the top of the converted output. The pre-port path avoided this by
# converting only ``<body>`` / ``<main>`` / ``<article>``; htmd converts the
# full document, so we drop ``<head>`` at the skip-tags layer instead.
_HTMD_SKIP_TAGS: list[str] = [*_NOISE_TAGS, "head"]


def _build_htmd_options():
    # ``htmd`` is a compiled PyO3 module with no .pyi stubs, so ty can't see
    # its public symbols. The runtime call is correct; suppress the static
    # check on the bare attribute lookups.
    opts = htmd.Options()  # ty: ignore[unresolved-attribute]
    opts.heading_style = "atx"
    opts.skip_tags = list(_HTMD_SKIP_TAGS)
    opts.image_placeholder = "[Image: {alt}]"
    opts.drop_empty_alt_images = True
    opts.drop_image_only_links = True
    return opts


_HTMD_OPTIONS = _build_htmd_options()

# Byte budget for the head-only BS4 fallback parse. 32 KB is well above any
# realistic <head> size and still parses in microseconds on html.parser.
_HEAD_SCAN_BYTES = 32 * 1024


def _extract_head_title(html: str) -> str:
    """Fallback title extraction for pages with no ``<h1>``.

    Parses only the first ``_HEAD_SCAN_BYTES`` of HTML to find ``og:title``
    or ``<title>``. Matches the ``og:title > <title> > "Untitled"`` tail of
    the prior title-resolution ladder; the h1-first step is handled by
    ``_FIRST_H1_RE`` over the converted markdown.
    """
    head_chunk = html[:_HEAD_SCAN_BYTES]
    soup = BeautifulSoup(head_chunk, "html.parser")
    og_title = soup.find("meta", property="og:title")
    og_content = str(og_title.get("content", "")).strip() if og_title else ""
    if og_content:
        return og_content
    title_tag = soup.find("title")
    if title_tag:
        text = title_tag.get_text(strip=True)
        if text:
            return text
    return "Untitled"


def html_to_markdown(html: str) -> tuple[str, str]:
    """Convert HTML to clean markdown, returning ``(title, markdown)``.

    Uses the Rust-backed ``htmd`` library with a flat ``Options``
    configuration that replicates the prior ``TextOnlyConverter`` semantics:
    noise tags are decomposed via ``skip_tags``, images render as
    ``[Image: alt]`` via ``image_placeholder``, empty-alt images are dropped,
    and image-only ``<a>`` wrappers are collapsed to their inner content.
    Measured speedup over the previous markdownify + BS4 path is roughly
    30x on small pages, 40x on megapage specs, and 50x on the pathological
    tier (the WHATWG HTML spec), while producing the full converted content
    at every size (no silent truncation).

    Title priority matches the prior behavior: the first ``<h1>`` in the
    converted markdown wins, falling back to ``og:title`` or ``<title>`` via
    a small head-only BS4 parse for pages with no h1, and finally
    ``"Untitled"``.
    """
    markdown = htmd.convert_html(html, _HTMD_OPTIONS)  # ty: ignore[unresolved-attribute]
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
    # Strip inline markup from heading lines once, at conversion time, so
    # downstream section-name extraction sees plain text without paying a
    # per-call strip cost. Matches the pre-port ``_clean_headings`` output.
    markdown = _HEADING_LINE_RE.sub(_strip_heading_line, markdown)

    # Skip ATX ``# X`` lines inside fenced code blocks — shell/Python/Make
    # comments in an example ``` block start with ``# `` and would otherwise
    # be captured as the document's title.  Spec documents are the concrete
    # motivator: WHATWG's real h1 lives inside ``<header>`` (decomposed by
    # skip_tags), and the first surviving ``# `` line is a bash comment
    # inside a ``<textarea>`` example (``# System-wide .bashrc file…``).
    # _find_fenced_code_ranges is the same helper
    # _extract_sections_from_markdown already uses to skip in-code headings.
    code_ranges = _find_fenced_code_ranges(markdown)
    first_h1 = None
    for m in _FIRST_H1_RE.finditer(markdown):
        if not any(start <= m.start() < end for start, end in code_ranges):
            first_h1 = m
            break
    title = first_h1.group(1).strip() if first_h1 else _extract_head_title(html)
    return title, markdown


# --- Truncation helper ---

def _apply_hard_truncation(
    content: str,
    max_tokens: int,
    hint_prefix: str = "Full page",
    hint_suffix: str = "Use max_tokens to adjust.",
) -> tuple[str, str | None]:
    """Apply token-limit truncation with a hard character cut.

    Best for non-markdown content (JSON, XML, plain text) where semantic
    boundaries are not meaningful.

    Returns (possibly_truncated_content, truncation_hint_or_none).
    """
    char_limit = max_tokens * 4
    if len(content) <= char_limit:
        return content, None

    total_kb = len(content) / 1024
    total_tokens_est = len(content) // 4
    truncated = content[:char_limit]
    hint = (
        f"{hint_prefix} is {total_kb:.1f} KB (~{total_tokens_est:,} tokens), "
        f"showing first ~{max_tokens:,} tokens. {hint_suffix}"
    )
    return truncated, hint


def _apply_semantic_truncation(
    content: str,
    max_tokens: int,
) -> tuple[str, str | None]:
    """Apply token-limit truncation at a semantic boundary.

    Uses MarkdownSplitter to find a clean break point (heading, paragraph
    boundary) rather than cutting mid-sentence.  Best for markdown content.

    Returns (possibly_truncated_content, truncation_hint_or_none).
    """

    char_limit = max_tokens * 4
    if len(content) <= char_limit:
        return content, None

    # trim=False guarantees "".join(chunks) == content, so packing a prefix
    # of chunks is lossless and requires no joiner (no \n\n math, no risk of
    # doubled blank lines).  This matters because MarkdownSplitter treats
    # headings as the highest-priority semantic boundary: content shaped like
    # "## Heading\n\n<large body>" where the body exceeds char_limit will
    # emit chunk 0 = "## Heading\n\n" alone, with the body in subsequent
    # chunks.  Returning chunks[0] would drop the entire body.
    splitter = MarkdownSplitter(char_limit, trim=False)
    chunks = splitter.chunks(content)

    if len(chunks) <= 1:
        return content, None

    # Pack chunks up to char_limit.  Two guards:
    #
    # * ``not packed`` — always return at least chunk 0, even if it already
    #   exceeds char_limit (pathological atomic chunk, e.g. a single
    #   megabyte-sized table row).
    # * ``used >= char_limit // 2`` — allow one chunk of soft overflow when
    #   the packed prefix is still small.  Without this, a tiny heading-alone
    #   chunk 0 (e.g. ``"## Film\n\n"``, 9 chars) paired with a body chunk
    #   that just barely crosses the budget would cause us to break after the
    #   heading, reproducing the original bug.  The splitter guarantees each
    #   chunk ≤ char_limit in normal cases, so the worst-case overflow is
    #   bounded at ~2×.
    packed: list[str] = []
    used = 0
    for chunk in chunks:
        next_size = used + len(chunk)
        if packed and next_size > char_limit and used >= char_limit // 2:
            break
        packed.append(chunk)
        used += len(chunk)
    truncated = "".join(packed).rstrip()

    shown_tokens = len(truncated) // 4
    total_kb = len(content) / 1024
    total_tokens_est = len(content) // 4
    hint = (
        f"Full page is {total_kb:.1f} KB (~{total_tokens_est:,} tokens), "
        f"showing first ~{shown_tokens:,} tokens. "
        "Use max_tokens to adjust, or section to fetch specific sections."
    )
    return truncated, hint


# --- Content fencing ---
#
# The fence implementation lives in betamatter; these aliases keep the
# names the rest of the package (and the tests) import from here.

_FENCE_OPEN = FENCE_OPEN
_FENCE_CLOSE = FENCE_CLOSE
_TRUST_ADVISORY = TRUST_ADVISORY


def _format_retraction_banner(
    retraction: dict | None, other_update: dict | None = None,
) -> str | None:
    """Render a prominent retraction / EoC / correction banner for paper bodies.

    Pass exactly one of ``retraction`` (shape:
    ``{notice_doi, date, source, label}``) or ``other_update`` (shape adds
    ``{type: "expression_of_concern" | "correction"}``).  If both are None,
    returns None.

    The returned string uses ASCII block quote formatting so it renders
    prominently regardless of theme.  Values are assumed pre-validated by
    the caller (``parkour_mcp.doi._extract_update_notice`` applies
    ``_DOI_SAFE_RE`` + printable-only filtering).
    """
    if retraction:
        tag = "[RETRACTED]"
        verb = "This paper has been retracted"
        entry = retraction
    elif other_update:
        if other_update.get("type") == "expression_of_concern":
            tag = "[EXPRESSION OF CONCERN]"
            verb = "The validity of this paper has been called into question"
        else:
            tag = "[CORRECTED]"
            verb = "This paper has been corrected"
        entry = other_update
    else:
        return None

    bits = [verb]
    if date := entry.get("date"):
        bits.append(f"on {date}")
    if notice_doi := entry.get("notice_doi"):
        bits.append(f"— notice: https://doi.org/{notice_doi}")
    source = entry.get("source")
    if source and source != "unknown":
        bits.append(f"(source: {source})")
    body = " ".join(bits) + "."
    if label := entry.get("label"):
        body += f"  \n> _{label}_"
    return f"> **{tag}** {body}"


# Label sanitization also lives in betamatter (fence heading choke point);
# aliased here for the section-extraction choke point below.
_sanitize_label = sanitize_label


def _fence_content(content: str, title: str | None = None) -> str:
    """Wrap content in an untrusted content fence with per-line provenance marking.

    Thin binding over ``betamatter.fence_content`` — box-drawing
    self-labeling delimiters, a │ prefix on every content line, and
    separator lines inside both boundaries (datamarking defense; see
    Microsoft Spotlighting and docs/frontmatter-standard.md "Content
    Fencing").

    Args:
        content: The untrusted content to fence (markdown, plain text, etc.)
        title: Optional page title to render as a heading inside the fence.
    """
    return fence_content(content, title=title)


# --- Section helpers ---

_HEADING_RE = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)
_FENCE_MARKER_RE = re.compile(r'^(`{3,}|~{3,})', re.MULTILINE)


def _find_fenced_code_ranges(text: str) -> list[tuple[int, int]]:
    """Find (start, end) char ranges of fenced code blocks in markdown.

    Linear-time scanner that tracks open/close state.  A closing fence must
    use the same character (` or ~) with at least as many repetitions as
    the opening fence, matching the CommonMark spec.
    """
    ranges: list[tuple[int, int]] = []
    open_start: int | None = None
    open_char: str | None = None
    open_count: int = 0

    for m in _FENCE_MARKER_RE.finditer(text):
        char = m.group(1)[0]
        count = len(m.group(1))

        if open_start is None:
            # Opening fence
            open_start = m.start()
            open_char = char
            open_count = count
        elif char == open_char and count >= open_count:
            # Closing fence — same char, at least as many repetitions
            ranges.append((open_start, m.end()))
            open_start = None
            open_char = None
            open_count = 0
        # Otherwise: different char or fewer repetitions — skip, stays open

    return ranges


# Matches any Unicode whitespace character that isn't a normal ASCII space.
# Covers &nbsp; (\u00a0), thin/hair/em/en spaces, zero-width spaces, etc.
_EXOTIC_WHITESPACE_RE = re.compile(r'[\u00a0\u2000-\u200b\u202f\u205f\u3000\ufeff]')


def _normalize_whitespace(text: str) -> str:
    """Collapse exotic Unicode whitespace variants to plain ASCII spaces."""
    return _EXOTIC_WHITESPACE_RE.sub(' ', text)


_SLUG_NON_ALNUM_RE = re.compile(r'[^a-z0-9]+')


def _slugify(text: str) -> str:
    """Convert heading text to a URL-fragment-style slug.

    Lowercase, collapse non-alphanumeric runs to single hyphens, strip
    leading/trailing hyphens.  Produces Goldmark-style slugs (underscores
    become hyphens).  GFM-style fragments (underscores preserved) are
    handled by the fuzzy fallback in _filter_markdown_by_sections.
    """
    return _SLUG_NON_ALNUM_RE.sub('-', text.lower()).strip('-')


# Matches a leading section-number token: one or more dot-separated
# runs of digits, optionally followed by a (possibly CommonMark-
# escaped) trailing period, then whitespace.  Examples: "13 ",
# "13.2 ", "13.2.6 ", "15. ", "15\\. ", "8.4.1.1. ".  Spec documents
# (WHATWG, ECMAScript, C++ draft, etc.) render section numbers as
# prose inside the heading text via ``<span class="secno">…</span>``;
# RFC Editor renders them as literal "15. " in the heading source,
# which htmd escapes to "15\\. " on conversion to keep the line from
# being read as an ordered list when pulled out of heading context.
# Either form should reduce to the descriptive title alone, so we
# register both ``"13.2.6 Tree construction"`` and ``"Tree
# construction"`` (plus their slugs) in the section lookup.
_SECTION_NUMBER_PREFIX_RE = re.compile(r"^\d+(?:\.\d+)*(?:\\?\.)?\s+")


def _strip_section_number(name: str) -> str:
    """Return *name* with a leading section-number token removed.

    If no leading number pattern is present, returns *name* unchanged.
    """
    return _SECTION_NUMBER_PREFIX_RE.sub("", name, count=1)


class MarkdownSection(TypedDict):
    """One heading and the span of markdown it owns.

    A plain ``dict`` here infers its value type as the union of everything
    stored (``str | int | bool``), so every positional use of ``start_pos`` /
    ``end_pos`` reads as possibly-``str`` and has to be re-narrowed at each
    call site.  Naming the shape once keeps the offsets typed as the integers
    they always were.
    """
    name: str
    level: int
    start_pos: int
    end_pos: int
    # NotRequired because it is derived metadata, not part of the span: every
    # consumer reads it through ``.get()``, and callers doing pure offset math
    # (ancestry breadcrumbs, slice mapping) build sections without it.
    header_only: NotRequired[bool]


def _extract_sections_from_markdown(markdown: str) -> list[MarkdownSection]:
    """Extract section headings from markdown text.

    Each section spans from its own heading to the next heading of any level,
    or to EOF for the last one.  Headings inside fenced code blocks (``` or
    ~~~) are skipped.
    """
    # Build set of character ranges inside fenced code blocks
    code_ranges = _find_fenced_code_ranges(markdown)

    def _inside_code(pos: int) -> bool:
        return any(start <= pos < end for start, end in code_ranges)

    # (name, level, start_pos) — end_pos needs the *next* heading's start, so
    # the full entries are assembled in the second pass below.
    found: list[tuple[str, int, int]] = []

    for match in _HEADING_RE.finditer(markdown):
        if _inside_code(match.start()):
            continue
        level = len(match.group(1))
        name = match.group(2).strip()
        # Normalize exotic whitespace (e.g. &nbsp;) and strip control
        # characters that could inject structure into output labels.
        name = _sanitize_label(_normalize_whitespace(name)).strip()
        if name and len(name) > 1:
            found.append((name, level, match.start()))

    sections: list[MarkdownSection] = []
    for i, (name, level, start_pos) in enumerate(found):
        end_pos = found[i + 1][2] if i + 1 < len(found) else len(markdown)
        # A section is header-only when everything after its heading line and
        # before the next heading is whitespace — its content lives in child
        # subsections rather than under the heading itself.
        heading_line_end = markdown.find("\n", start_pos)
        if heading_line_end == -1:
            heading_line_end = len(markdown)
        entry: MarkdownSection = {
            "name": name,
            "level": level,
            "start_pos": start_pos,
            "end_pos": end_pos,
            "header_only": not markdown[heading_line_end:end_pos].strip(),
        }
        sections.append(entry)

    return sections


def _find_parent_idx(sections: list[MarkdownSection], idx: int) -> int | None:
    """Find the index of the nearest ancestor section (lower heading level)."""
    target_level = sections[idx]["level"]
    for j in range(idx - 1, -1, -1):
        if sections[j]["level"] < target_level:
            return j
    return None


def _name_counts(sections: list[MarkdownSection]) -> dict[str, int]:
    """Count occurrences of each section name for disambiguation."""
    counts: dict[str, int] = {}
    for s in sections:
        counts[s["name"]] = counts.get(s["name"], 0) + 1
    return counts


# Default page size for TOC pagination.  Hardcoded rather than a
# parameter on web_fetch_sections — at ~70-80 chars/line for typical
# RFC/spec section names, 100 sections lands around 2-3K tokens which
# fits comfortably under any reasonable max_tokens budget.  Callers
# walk longer documents via the slice= parameter.
_TOC_SLICE_SIZE = 100


def _build_section_list(
    sections: list[MarkdownSection],
    max_sections: int = 100,
    include_slugs: bool = False,
    *,
    start: int = 0,
) -> list[str]:
    """Build indented section list for display.

    Disambiguates duplicate names by appending (Parent Name).
    Returns list of formatted strings like "  - Section Name".
    When include_slugs is True, appends the anchor slug: "  - Section Name (#slug)".

    *start* (keyword-only) offsets the window so the same section list
    can be paginated.  Indentation and disambiguation are computed
    against the full *sections* list so the rendered window stays
    coherent even when it begins mid-document.  When *start* > 0,
    a leading "... and N earlier sections" sentinel is prepended;
    when the window doesn't reach the end, a trailing "... and N
    more sections" sentinel is appended.
    """
    if not sections:
        return []

    min_level = min(s["level"] for s in sections)
    counts = _name_counts(sections)

    end = min(start + max_sections, len(sections))
    lines = []

    if start > 0:
        lines.append(f"# ... and {start} earlier sections")

    for i in range(start, end):
        sec = sections[i]
        indent = (sec["level"] - min_level) * 2
        name = sec["name"]
        if counts[name] > 1:
            parent_idx = _find_parent_idx(sections, i)
            if parent_idx is not None:
                name = f"{name} ({sections[parent_idx]['name']})"
        slug_suffix = f" (#{_slugify(sec['name'])})" if include_slugs else ""
        ho_suffix = " [header only]" if sec.get("header_only") else ""
        lines.append(" " * indent + f"- {name}{slug_suffix}{ho_suffix}")

    if end < len(sections):
        remaining = len(sections) - end
        lines.append(f"# ... and {remaining} more sections")

    return lines


def _resolve_toc_slice(
    total_sections: int, slice_index: int, slice_size: int = _TOC_SLICE_SIZE,
) -> dict:
    """Resolve a (possibly negative or out-of-range) slice index to a window.

    Returns a dict with:
      - ``start``: section-list index where the window begins
      - ``effective_slice``: the post-clamp, post-negative-resolution slice
        index (always non-negative, always in [0, total_slices))
      - ``total_slices``: how many slices the document divides into
      - ``clamped_from``: the original *slice_index* if clamping or negative
        resolution changed it, else ``None``

    Empty section lists return a single zero-length slice at index 0.
    """
    if total_sections <= 0:
        return {
            "start": 0,
            "effective_slice": 0,
            "total_slices": 0,
            "clamped_from": None,
        }

    total_slices = (total_sections + slice_size - 1) // slice_size
    requested = slice_index

    # Resolve Python-style negative index against the slice count
    if slice_index < 0:
        slice_index = total_slices + slice_index

    # Clamp out-of-range (positive overflow OR still-negative-after-resolution)
    if slice_index < 0:
        slice_index = 0
    elif slice_index >= total_slices:
        slice_index = total_slices - 1

    # Clamping reports the original input.  Clean negative-index
    # resolution (e.g. slice=-1 on a 3-slice document → 2) is the
    # feature working correctly and doesn't need a note — the caller
    # can verify by reading the returned ``effective_slice``.
    clamped_from = requested if slice_index != requested and not (
        requested < 0 and 0 <= total_slices + requested < total_slices
    ) else None

    return {
        "start": slice_index * slice_size,
        "effective_slice": slice_index,
        "total_slices": total_slices,
        "clamped_from": clamped_from,
    }


def _compute_slice_ancestry(
    sections: list[MarkdownSection], chunk_offsets: list[int],
) -> list[str]:
    """Map each chunk's character offset to a section ancestry breadcrumb.

    Returns one string per chunk, e.g. "Background > Historical Context (2/3)".
    When consecutive chunks share the same innermost section, a positional
    hint (N/M) is appended so the reader knows where they are within a large
    section.  Single-slice sections get no hint.  Chunks before the first
    heading produce an empty string.
    """
    if not chunk_offsets:
        return []

    # --- Step 1: find the innermost section index for each chunk ---
    sec_indices: list[int | None] = []
    for offset in chunk_offsets:
        found: int | None = None
        for si in range(len(sections) - 1, -1, -1):
            if sections[si]["start_pos"] <= offset:
                found = si
                break
        sec_indices.append(found)

    # --- Step 2: build raw ancestry paths (without positional hints) ---
    def _ancestry_path(sec_idx: int | None) -> str:
        if sec_idx is None:
            return ""
        parts = [sections[sec_idx]["name"]]
        current = sec_idx
        while True:
            parent = _find_parent_idx(sections, current)
            if parent is None:
                break
            parts.insert(0, sections[parent]["name"])
            current = parent
        return " > ".join(parts)

    raw_paths = [_ancestry_path(si) for si in sec_indices]

    # --- Step 3: group consecutive chunks sharing the same section ---
    # Walk the list and identify runs of identical sec_indices.
    # For runs longer than 1, append (pos/total) to each.
    result = list(raw_paths)  # copy for mutation
    i = 0
    while i < len(sec_indices):
        si = sec_indices[i]
        # Find end of run
        j = i + 1
        while j < len(sec_indices) and sec_indices[j] == si:
            j += 1
        run_len = j - i
        if run_len > 1 and si is not None:
            for k in range(i, j):
                pos = k - i + 1
                result[k] = f"{raw_paths[k]} ({pos}/{run_len})"
        i = j

    return result


def _filter_markdown_by_sections(
    markdown: str,
    section_names: list[str],
    sections: list[MarkdownSection],
) -> tuple[str, list[dict], list[str]]:
    """Filter markdown to only include requested sections.

    Matches requested names against section list (case-sensitive exact match,
    including disambiguation suffix from _build_section_list).

    Returns (filtered_markdown, [{name, ancestry_path}], [unmatched_names]).
    """
    counts = _name_counts(sections)

    def _get_display_name(idx: int) -> str:
        name = sections[idx]["name"]
        if counts[name] > 1:
            pidx = _find_parent_idx(sections, idx)
            if pidx is not None:
                return f"{name} ({sections[pidx]['name']})"
        return name

    def _build_ancestry(idx: int) -> str:
        """Build ancestry path like 'Grandparent > Parent > Name'."""
        path = [sections[idx]["name"]]
        current = idx
        while True:
            parent = _find_parent_idx(sections, current)
            if parent is None:
                break
            path.insert(0, sections[parent]["name"])
            current = parent
        return " > ".join(path)

    # Map display names and raw names to section indices.  Also register
    # the name with a leading section-number token stripped (e.g.
    # "Tree construction" alongside "13.2.6 Tree construction") so
    # callers can refer to spec sections by their human-readable title
    # alone.  ``setdefault`` means the first section whose stripped
    # form produces a given key wins — subsequent collisions are
    # resolvable via the disambiguated display name or the explicit
    # full name.
    display_to_idx: dict[str, int] = {}
    for i in range(len(sections)):
        name = sections[i]["name"]
        display_to_idx[_get_display_name(i)] = i
        # Also map raw name for non-ambiguous sections
        display_to_idx.setdefault(name, i)
        stripped = _strip_section_number(name)
        if stripped and stripped != name:
            display_to_idx.setdefault(stripped, i)

    # Slug lookup: maps slugified heading text to section index.  Same
    # number-stripping treatment so ``section=tree-construction`` (the
    # bare slug a caller might copy out of a URL fragment or build from
    # the human-readable name) resolves against
    # ``13.2.6 Tree construction``.
    slug_to_idx: dict[str, int] = {}
    for i in range(len(sections)):
        name = sections[i]["name"]
        slug = _slugify(name)
        if slug:
            slug_to_idx.setdefault(slug, i)
        stripped = _strip_section_number(name)
        if stripped and stripped != name:
            stripped_slug = _slugify(stripped)
            if stripped_slug:
                slug_to_idx.setdefault(stripped_slug, i)

    def _has_subsections(idx: int) -> bool:
        """Check if the section at idx has child sections (deeper level)."""
        if idx + 1 < len(sections):
            return sections[idx + 1]["level"] > sections[idx]["level"]
        return False

    def _match(idx: int, fragment: str | None = None) -> dict:
        meta: dict = {
            "name": sections[idx]["name"],
            "ancestry_path": _build_ancestry(idx),
        }
        if fragment is not None:
            meta["matched_fragment"] = fragment
        if _has_subsections(idx):
            meta["has_subsections"] = True
        if sections[idx].get("header_only"):
            meta["header_only"] = True
        return meta

    matched_parts = []
    matched_meta = []
    unmatched = []

    for req_name in section_names:
        req_name = _normalize_whitespace(req_name)
        if req_name in display_to_idx:
            idx = display_to_idx[req_name]
            sec = sections[idx]
            matched_parts.append(markdown[sec["start_pos"]:sec["end_pos"]].strip())
            matched_meta.append(_match(idx))
        elif req_name in slug_to_idx:
            idx = slug_to_idx[req_name]
            sec = sections[idx]
            matched_parts.append(markdown[sec["start_pos"]:sec["end_pos"]].strip())
            matched_meta.append(_match(idx, fragment=req_name))
        else:
            # Fuzzy fallback: slugify the fragment so it matches the same
            # canonical form as the heading slugs in slug_to_idx.  Handles
            # case folding, underscore↔hyphen (GFM vs Goldmark), percent-
            # encoded characters, apostrophes, and other punctuation that
            # different platforms preserve or strip in URLs.
            fuzzy = _slugify(unquote(req_name))
            if fuzzy and fuzzy in slug_to_idx:
                idx = slug_to_idx[fuzzy]
                sec = sections[idx]
                matched_parts.append(markdown[sec["start_pos"]:sec["end_pos"]].strip())
                matched_meta.append(_match(idx, fragment=req_name))
            else:
                unmatched.append(req_name)

    result = "\n\n".join(matched_parts)
    return result, matched_meta, unmatched


# ---------------------------------------------------------------------------
# Tip registry and fire-once ledger
#
# ``tip`` frontmatter carries an educational, once-per-session lesson — see
# docs/frontmatter-standard.md "tip semantics".  The mechanics (fire-once
# ledger, ``::``-scoped ledger IDs, render-time dedup) live in
# ``betamatter.TipLedger``; parkour owns the registry content and injects
# ``tool_name`` as the placeholder resolver:
#
#   _TIPS        registry mapping a stable tip ID to its canonical text.
#                Tips are content-addressable by ID: callers emit an ID via
#                ``FMEntries.set_tip()``, never a string, so templated or
#                variable content cannot reach a ``tip`` field.
#   _tip_ledger  the process-scoped ledger; a tip fires at most once, and
#                URL-scoped tips fire once per URL.
#   _FIRED_TIPS  alias of the ledger's fired-set.  Process-lifetime by
#                design, reset only on server restart; tests clear it
#                per-test via an autouse fixture in conftest.py.
#
# Registry values may carry ``{tool_key}`` placeholders (e.g.
# ``{web_fetch_sections}``); these resolve to the active profile's display
# name, a launch-time constant, so the rendered tip is still deterministic
# per process — the property the fire-once ledger relies on.
_TIPS: dict[str, str] = {
    "webfetchsections_scout": (
        "{web_fetch_sections} returns the page's heading layout; use it to "
        "scout before committing to a content fetch"
    ),
    "incisive_premature_playwright": (
        "{web_fetch_direct} detects JavaScript-shell pages on its own; set "
        "requires_js after a plain fetch comes back empty, not preemptively"
    ),
}

_tip_ledger = TipLedger(registry=_TIPS, resolver=tool_name)

_FIRED_TIPS: set[str] = _tip_ledger.fired


class FMEntries(_BetamatterFMEntries):
    """Frontmatter-entries dict that routes multi-contributor keys
    through ``append`` so concurrent advisories compose instead of
    clobbering.

    Thin parkour binding of ``betamatter.FMEntries`` (see that class
    for the mutation-path guard mechanics and the UserDict rationale):
    it pins the tip ledger to this module's ``_tip_ledger`` — so
    ``set_tip`` validates against ``_TIPS`` — and keeps parkour's
    ``url=`` spelling for per-URL tip scoping.

    Multi-contributor keys — ``hint``, ``warning``, ``note``,
    ``see_also``, ``alert`` — can receive contributions from multiple
    subsystems (fragment resolution, search-parser warnings,
    pagination hints, etc.) in a single request.  Direct ``d[key] =
    value`` raises; use ``d.append(key, value)`` or the free helper
    ``_append_frontmatter_entry``.  ``update`` / ``|=`` route protected
    keys through ``append`` automatically.

    PROTECTED_ORDER (inherited) is the canonical presentation sequence
    used in docs/frontmatter-standard.md; the library owns the tuple and
    scripts/cog_helpers.protected_keys() consumes it from betamatter to
    keep the doc's prose, count, and table in sync.
    """

    tip_ledger = _tip_ledger

    # Deliberately narrows the base's ``scope=`` keyword to ``url=``: in
    # parkour every tip scope *is* a URL, and the domain spelling is what
    # call sites read better as.  That is a real Liskov violation — code
    # holding a ``betamatter.FMEntries`` reference and passing ``scope=``
    # would fail on this subclass — so it is suppressed rather than
    # unexplained.  Safe today because no call site outside the ``super()``
    # delegation below passes ``scope=``; if one ever needs to, accept both
    # spellings here rather than silencing a second checker.
    def set_tip(  # ty: ignore[invalid-method-override]
        self, tip_id: str, *, url: str | None = None,
    ) -> None:
        """Emit a single educational tip on this frontmatter build.

        Unlike the protected keys (which append), ``tip`` is single-write
        per build: a second ``set_tip`` call raises.  *tip_id* must be a
        base ID registered in ``_TIPS``.

        When *url* is given the tip is scoped per-URL — it fires once for
        each distinct URL rather than once per process — by suffixing the
        ledger ID with a digest of the URL.  The fire-once dedup itself
        happens at render time in ``_build_frontmatter``; ``set_tip`` only
        records intent.
        """
        super().set_tip(tip_id, scope=url)


def _append_frontmatter_entry(fm_entries, key: str, value) -> None:
    """Append a value to an ``fm_entries`` field, promoting scalar→list as needed.

    Thin binding over ``betamatter.append_frontmatter_entry``.  Empty
    (``None`` / falsy) values are ignored.  The first value lands as a
    scalar; each subsequent call promotes the field to a YAML sequence.
    ``_build_frontmatter`` renders single-item lists as scalars and
    multi-item lists as YAML sequences (see frontmatter-standard.md
    "List Values"), so callers can append without worrying about the
    resulting shape.

    Use this (or ``FMEntries.append``) instead of inline
    ``fm_entries[key] = ...`` whenever more than one subsystem can
    contribute to the same key.  Works against both ``FMEntries`` and
    plain ``dict`` so it stays usable in tests and transitional code.
    """
    append_frontmatter_entry(fm_entries, key, value)


def _build_frontmatter(
    entries: Mapping,
    sections_not_found: list[str] | None = None,
) -> str:
    """Build YAML frontmatter block.

    Thin binding over ``betamatter.build_frontmatter`` (None-skipping,
    adaptive scalar/list rendering, one line per scalar) that re-adds
    parkour's ``sections_not_found`` special case.

    Attacker-controlled section metadata (names, matched fragments,
    ancestry paths, sections_available truncation hints) used to live
    in the frontmatter but was moved into the fenced content zone in
    commits a0ec740 and fa714ee — anything derived from page headings
    belongs in the untrusted zone, not the trusted server-generated one.

    A ``tip`` entry is resolved through the registry and fire-once ledger
    (``_tip_ledger``): an already-fired or unregistered tip renders
    nothing, and rendering a tip marks it spent.  This is a deliberate
    side effect of the build — the fire-once guarantee is keyed to actual
    output, per docs/frontmatter-standard.md "tip semantics".

    Args:
        entries: Key-value pairs for frontmatter (None values are skipped).
        sections_not_found: Section names that were requested but not
            matched. These come from the user's request parameter, not
            from page content, so they stay in the trusted zone.
    """
    fm = build_frontmatter(entries, tip_ledger=_tip_ledger)
    if sections_not_found:
        lines = fm.split("\n")
        extra = ["sections_not_found:"]
        extra.extend(f"  - \"{name}\"" for name in sections_not_found)
        lines[-1:-1] = extra
        fm = "\n".join(lines)
    return fm


# ---------------------------------------------------------------------------
# Plaintext presplitting
# ---------------------------------------------------------------------------
# Generic, format-agnostic line chunker for cache presplitting.  Lives here
# rather than in a tool module because it has no source-specific behavior and
# more than one tool needs it: the GitHub blob path uses it as the fallback
# under tree-sitter, and the HuggingFace file path uses it directly (none of
# the formats that tool reads — json, yaml, txt — have a tree-sitter grammar
# registered, so the AST branch would never fire for them).

# Circuit-breaker threshold: any single line longer than this is treated as
# pathological and trips ``_plaintext_presplit``.  Real log/code lines are
# typically <1 KB; the longest legitimate single-line content we see in the
# wild is minified JavaScript bundles, which can approach but rarely exceed
# 1 MB.  Anything beyond is almost certainly adversarial or a data dump —
# dropping cache support for that file is preferable to letting
# MarkdownSplitter's char-level fallback spend multi-second CPU bursts on
# unstructured single-line content (see issue #6).
_MAX_PLAINTEXT_LINE_CHARS = 1_000_000

# Target size of each presplit chunk for plaintext content.  Matches the
# midpoint of ``MarkdownSplitter((1600, 2000))`` so BM25 index behavior is
# consistent across structured and unstructured cache entries.
_PLAINTEXT_CHUNK_CHARS = 1800


def _plaintext_presplit(
    source: str,
    chunk_chars: int = _PLAINTEXT_CHUNK_CHARS,
    max_line_chars: int = _MAX_PLAINTEXT_LINE_CHARS,
) -> list[tuple[int, str]] | None:
    """Line-oriented presplit for plaintext blobs with no tree-sitter grammar.

    Groups consecutive lines into chunks up to ``chunk_chars`` in length,
    always splitting on line boundaries so BM25 slices stay readable.
    Returns (char_offset, chunk_text) tuples suitable for
    _PageCache.store(presplit=...).

    Circuit breaker: if any single line exceeds ``max_line_chars``, the
    content is treated as pathological (typical vector: a multi-MiB data
    dump emitted as one line) and the function returns ``None``.  Callers
    should then skip cache population entirely — MarkdownSplitter's
    char-level fallback on unbounded single-line content is the DoS path
    filed as issue #6, and returning None avoids routing into it.
    """
    chunks: list[tuple[int, str]] = []
    n = len(source)
    pos = 0
    chunk_start = 0
    chunk_buf: list[str] = []
    chunk_size = 0

    while pos < n:
        nl = source.find("\n", pos)
        line_end = n if nl == -1 else nl + 1  # include the newline
        line_len = line_end - pos

        if line_len > max_line_chars:
            return None  # circuit breaker — see docstring

        if chunk_size > 0 and chunk_size + line_len > chunk_chars:
            chunks.append((chunk_start, "".join(chunk_buf)))
            chunk_buf = []
            chunk_size = 0
            chunk_start = pos

        chunk_buf.append(source[pos:line_end])
        chunk_size += line_len
        pos = line_end

    if chunk_buf:
        chunks.append((chunk_start, "".join(chunk_buf)))

    return chunks
