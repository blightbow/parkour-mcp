"""HTML-to-markdown conversion and section extraction helpers."""

import re
from typing import Optional
from urllib.parse import unquote

from bs4 import BeautifulSoup
from markdownify import MarkdownConverter


class TextOnlyConverter(MarkdownConverter):
    """Custom converter that preserves link hrefs but strips non-text content like images."""

    def convert_img(self, el, text, parent_tags):
        # Images can't render as text - return alt text only if meaningful
        alt = el.get('alt', '').strip()
        return f'[Image: {alt}]' if alt else ''

    def convert_a(self, el, text, parent_tags):
        # If the link only contains an image reference, drop it entirely
        stripped = text.strip()
        if stripped.startswith('[Image:') or not stripped:
            return stripped
        # Otherwise use default link conversion
        return super().convert_a(el, text, parent_tags)


def md(html, **options):
    """Convert HTML to markdown using custom converter."""
    return TextOnlyConverter(**options).convert(html)


# Noise tags removed before markdown conversion
_NOISE_TAGS = ["script", "style", "nav", "header", "footer", "aside", "noscript"]


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


def html_to_markdown(html: str) -> tuple[str, str]:
    """Convert HTML to clean markdown, returning (title, markdown).

    Removes noise elements, finds main content area, converts to markdown,
    and collapses excessive newlines.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Title priority: h1 (visible article heading) > og:title (clean metadata) >
    # <title> (often polluted with site name, breadcrumbs, separators)
    h1 = soup.find("h1")
    og_title = soup.find("meta", property="og:title")
    title_tag = soup.find("title")
    if h1:
        title = h1.get_text(strip=True)
    elif og_title and og_title.get("content", "").strip():
        title = og_title["content"].strip()
    elif title_tag:
        title = title_tag.get_text(strip=True)
    else:
        title = "Untitled"

    for tag in soup(_NOISE_TAGS):
        tag.decompose()

    _clean_headings(soup)

    main = soup.find("main") or soup.find("article") or soup.find("body") or soup
    markdown = md(str(main), heading_style="ATX")
    markdown = re.sub(r'\n{3,}', '\n\n', markdown).strip()
    return title, markdown


# --- Truncation helper ---

def _apply_truncation(
    content: str,
    max_tokens: int,
    hint_prefix: str = "Full page",
    hint_suffix: str = "Use max_tokens to adjust, section to fetch specific sections, "
                       "or kagi_summarize for a summary.",
) -> tuple[str, Optional[str]]:
    """Apply token-limit truncation to content.

    Returns (possibly_truncated_content, truncation_hint_or_none).
    """
    char_limit = max_tokens * 4
    if len(content) <= char_limit:
        return content, None

    total_kb = len(content) / 1024
    total_tokens_est = len(content) // 4
    truncated = content[:char_limit] + "\n\n[content truncated]"
    hint = (
        f"{hint_prefix} is {total_kb:.1f} KB (~{total_tokens_est:,} tokens), "
        f"showing first ~{max_tokens:,} tokens. {hint_suffix}"
    )
    return truncated, hint


# --- Section helpers ---

_HEADING_RE = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)
_FENCED_CODE_RE = re.compile(r'^(`{3,}|~{3,}).*?\n.*?^\1', re.MULTILINE | re.DOTALL)

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


def _extract_sections_from_markdown(markdown: str) -> list[dict]:
    """Extract section headings from markdown text.

    Returns list of {name, level, start_pos, end_pos} dicts.
    Skips headings inside fenced code blocks (``` or ~~~).
    """
    # Build set of character ranges inside fenced code blocks
    code_ranges = [
        (m.start(), m.end()) for m in _FENCED_CODE_RE.finditer(markdown)
    ]

    def _inside_code(pos: int) -> bool:
        return any(start <= pos < end for start, end in code_ranges)

    sections = []

    for match in _HEADING_RE.finditer(markdown):
        if _inside_code(match.start()):
            continue
        level = len(match.group(1))
        name = match.group(2).strip()
        # Normalize exotic whitespace (e.g. &nbsp;) — inline markup is
        # already cleaned by _clean_headings() before markdown conversion
        name = _normalize_whitespace(name).strip()
        if name and len(name) > 1:
            sections.append({
                "name": name,
                "level": level,
                "start_pos": match.start(),
            })

    # Compute end positions (each section ends where the next begins, or at EOF)
    for i, sec in enumerate(sections):
        if i + 1 < len(sections):
            sec["end_pos"] = sections[i + 1]["start_pos"]
        else:
            sec["end_pos"] = len(markdown)

    return sections


def _find_parent_idx(sections: list[dict], idx: int) -> Optional[int]:
    """Find the index of the nearest ancestor section (lower heading level)."""
    target_level = sections[idx]["level"]
    for j in range(idx - 1, -1, -1):
        if sections[j]["level"] < target_level:
            return j
    return None


def _name_counts(sections: list[dict]) -> dict[str, int]:
    """Count occurrences of each section name for disambiguation."""
    counts: dict[str, int] = {}
    for s in sections:
        counts[s["name"]] = counts.get(s["name"], 0) + 1
    return counts


def _build_section_list(
    sections: list[dict], max_sections: int = 100, include_slugs: bool = False,
) -> list[str]:
    """Build indented section list for display.

    Disambiguates duplicate names by appending (Parent Name).
    Returns list of formatted strings like "  - Section Name".
    When include_slugs is True, appends the anchor slug: "  - Section Name (#slug)".
    """
    if not sections:
        return []

    min_level = min(s["level"] for s in sections)
    counts = _name_counts(sections)

    lines = []
    for i, sec in enumerate(sections):
        if i >= max_sections:
            remaining = len(sections) - max_sections
            lines.append(f"# ... and {remaining} more sections")
            break
        indent = (sec["level"] - min_level) * 2
        name = sec["name"]
        if counts[name] > 1:
            parent_idx = _find_parent_idx(sections, i)
            if parent_idx is not None:
                name = f"{name} ({sections[parent_idx]['name']})"
        slug_suffix = f" (#{_slugify(sec['name'])})" if include_slugs else ""
        lines.append(" " * indent + f"- {name}{slug_suffix}")

    return lines


def _filter_markdown_by_sections(
    markdown: str,
    section_names: list[str],
    sections: list[dict],
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

    # Map display names and raw names to section indices
    display_to_idx: dict[str, int] = {}
    for i in range(len(sections)):
        display_to_idx[_get_display_name(i)] = i
        # Also map raw name for non-ambiguous sections
        display_to_idx[sections[i]["name"]] = i

    # Slug lookup: maps slugified heading text to section index
    slug_to_idx: dict[str, int] = {}
    for i in range(len(sections)):
        slug = _slugify(sections[i]["name"])
        if slug:
            slug_to_idx.setdefault(slug, i)

    matched_parts = []
    matched_meta = []
    unmatched = []

    for req_name in section_names:
        req_name = _normalize_whitespace(req_name)
        if req_name in display_to_idx:
            idx = display_to_idx[req_name]
            sec = sections[idx]
            matched_parts.append(markdown[sec["start_pos"]:sec["end_pos"]].strip())
            matched_meta.append({
                "name": sec["name"],
                "ancestry_path": _build_ancestry(idx),
            })
        elif req_name in slug_to_idx:
            idx = slug_to_idx[req_name]
            sec = sections[idx]
            matched_parts.append(markdown[sec["start_pos"]:sec["end_pos"]].strip())
            matched_meta.append({
                "name": sec["name"],
                "ancestry_path": _build_ancestry(idx),
                "matched_fragment": req_name,
            })
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
                matched_meta.append({
                    "name": sec["name"],
                    "ancestry_path": _build_ancestry(idx),
                    "matched_fragment": req_name,
                })
            else:
                unmatched.append(req_name)

    result = "\n\n".join(matched_parts)
    return result, matched_meta, unmatched


def _build_frontmatter(
    entries: dict,
    sections_requested: Optional[list[dict]] = None,
    sections_not_found: Optional[list[str]] = None,
    sections_available: Optional[list[str]] = None,
) -> str:
    """Build YAML frontmatter block.

    Args:
        entries: Key-value pairs for frontmatter (None values are skipped).
        sections_requested: Matched section metadata from _filter_markdown_by_sections.
            Takes precedence over sections_available if both are provided.
        sections_not_found: Section names that were requested but not matched.
        sections_available: Formatted section list from _build_section_list (for truncation hints).
    """
    lines = ["---"]
    for key, value in entries.items():
        if value is not None:
            lines.append(f"{key}: {value}")

    if sections_requested:
        if len(sections_requested) == 1:
            sec = sections_requested[0]
            lines.append(f"# {sec['ancestry_path']}")
            lines.append(f"section: {sec['name']}")
            if sec.get("matched_fragment"):
                lines.append(f"matched_fragment: \"#{sec['matched_fragment']}\"")
        else:
            lines.append("sections:")
            for sec in sections_requested:
                lines.append(f"  # {sec['ancestry_path']}")
                if sec.get("matched_fragment"):
                    lines.append(f"  - {sec['name']}  # matched #{sec['matched_fragment']}")
                else:
                    lines.append(f"  - {sec['name']}")
    elif sections_available:
        lines.append("sections:")
        for entry in sections_available:
            lines.append(f"  {entry}")

    if sections_not_found:
        lines.append("sections_not_found:")
        for name in sections_not_found:
            lines.append(f"  - \"{name}\"")

    lines.append("---")
    return "\n".join(lines)
