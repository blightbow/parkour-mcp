"""HTML-to-markdown conversion and section extraction helpers."""

import re
from typing import Optional

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


def html_to_markdown(html: str) -> tuple[str, str]:
    """Convert HTML to clean markdown, returning (title, markdown).

    Removes noise elements, finds main content area, converts to markdown,
    and collapses excessive newlines.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Extract title before decomposing elements
    title_tag = soup.find("title") or soup.find("h1")
    title = title_tag.get_text(strip=True) if title_tag else "Untitled"

    for tag in soup(_NOISE_TAGS):
        tag.decompose()

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


def _extract_sections_from_markdown(markdown: str) -> list[dict]:
    """Extract section headings from markdown text.

    Returns list of {name, level, start_pos, end_pos} dicts.
    """
    sections = []

    for match in _HEADING_RE.finditer(markdown):
        level = len(match.group(1))
        name = match.group(2).strip()
        # Clean heading text: strip bold markers, [edit] links, trailing whitespace
        name = re.sub(r'\*+', '', name)
        name = re.sub(r'\[edit\]', '', name, flags=re.IGNORECASE)
        name = name.strip()
        if name:
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


def _build_section_list(sections: list[dict], max_sections: int = 50) -> list[str]:
    """Build indented section list for display.

    Disambiguates duplicate names by appending (Parent Name).
    Returns list of formatted strings like "  - Section Name".
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
        lines.append(" " * indent + f"- {name}")

    return lines


def _filter_markdown_by_sections(
    markdown: str,
    section_names: list[str],
    sections: list[dict],
) -> tuple[str, list[dict]]:
    """Filter markdown to only include requested sections.

    Matches requested names against section list (case-sensitive exact match,
    including disambiguation suffix from _build_section_list).

    Returns (filtered_markdown, [{name, ancestry_path}]).
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

    # Map display names to section indices
    display_to_idx: dict[str, int] = {}
    for i in range(len(sections)):
        display_to_idx[_get_display_name(i)] = i
        # Also map raw name for non-ambiguous sections
        display_to_idx[sections[i]["name"]] = i

    matched_parts = []
    matched_meta = []
    unmatched = []

    for req_name in section_names:
        if req_name in display_to_idx:
            idx = display_to_idx[req_name]
            sec = sections[idx]
            matched_parts.append(markdown[sec["start_pos"]:sec["end_pos"]].strip())
            matched_meta.append({
                "name": sec["name"],
                "ancestry_path": _build_ancestry(idx),
            })
        else:
            unmatched.append(req_name)

    result = "\n\n".join(matched_parts)
    if unmatched:
        result += "\n\n<!-- sections not found: " + ", ".join(unmatched) + " -->"

    return result, matched_meta


def _build_frontmatter(
    entries: dict,
    sections_requested: Optional[list[dict]] = None,
    sections_available: Optional[list[str]] = None,
) -> str:
    """Build YAML frontmatter block.

    Args:
        entries: Key-value pairs for frontmatter (None values are skipped).
        sections_requested: Matched section metadata from _filter_markdown_by_sections.
            Takes precedence over sections_available if both are provided.
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
        else:
            lines.append("sections:")
            for sec in sections_requested:
                lines.append(f"  # {sec['ancestry_path']}")
                lines.append(f"  - {sec['name']}")
    elif sections_available:
        lines.append("sections:")
        for entry in sections_available:
            lines.append(f"  {entry}")

    lines.append("---")
    return "\n".join(lines)
