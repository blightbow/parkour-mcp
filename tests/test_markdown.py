"""Tests for parkour_mcp.markdown module."""

from parkour_mcp.markdown import (
    md,
    html_to_markdown,
    _slugify,
    _extract_sections_from_markdown,
    _build_section_list,
    _filter_markdown_by_sections,
    _build_frontmatter,
    _apply_hard_truncation,
    _apply_semantic_truncation,
    _fence_content,
    _format_retraction_banner,
    _FENCE_OPEN,
    _FENCE_CLOSE,
)

from .conftest import SAMPLE_MARKDOWN, SAMPLE_MARKDOWN_WITH_DUPLICATES


# --- md() / TextOnlyConverter ---

class TestMd:
    def test_basic_html_to_markdown(self):
        result = md("<h1>Hello</h1><p>World</p>")
        assert "Hello" in result
        assert "World" in result

    def test_strips_images_without_alt(self):
        result = md('<img src="photo.jpg">')
        assert result.strip() == ""

    def test_preserves_image_alt_text(self):
        result = md('<img src="photo.jpg" alt="A cat">')
        assert "[Image: A cat]" in result

    def test_preserves_link_hrefs(self):
        result = md('<a href="https://example.com">Click here</a>')
        assert "Click here" in result
        assert "https://example.com" in result

    def test_drops_image_only_links(self):
        result = md('<a href="https://example.com"><img src="x.jpg" alt="Logo"></a>')
        assert "[Image: Logo]" in result
        assert "https://example.com" not in result

    def test_drops_empty_links(self):
        result = md('<a href="https://example.com"></a>')
        assert "https://example.com" not in result

    def test_heading_style_atx(self):
        result = md("<h2>Heading</h2>", heading_style="ATX")
        assert "## Heading" in result


# --- _extract_sections_from_markdown ---

class TestExtractSections:
    def test_extracts_all_levels(self):
        sections = _extract_sections_from_markdown(SAMPLE_MARKDOWN)
        names = [s["name"] for s in sections]
        assert names == ["Main Title", "Section One", "Section Two", "Subsection A", "Section Three"]

    def test_correct_levels(self):
        sections = _extract_sections_from_markdown(SAMPLE_MARKDOWN)
        levels = [s["level"] for s in sections]
        assert levels == [1, 2, 2, 3, 2]

    def test_end_pos_chains(self):
        sections = _extract_sections_from_markdown(SAMPLE_MARKDOWN)
        # Each section's end_pos should equal next section's start_pos
        for i in range(len(sections) - 1):
            assert sections[i]["end_pos"] == sections[i + 1]["start_pos"]
        # Last section ends at EOF
        assert sections[-1]["end_pos"] == len(SAMPLE_MARKDOWN)

    def test_start_pos_within_content(self):
        sections = _extract_sections_from_markdown(SAMPLE_MARKDOWN)
        for sec in sections:
            # The heading text should appear at start_pos
            assert SAMPLE_MARKDOWN[sec["start_pos"]:].startswith("#")

    def test_empty_markdown(self):
        assert _extract_sections_from_markdown("") == []

    def test_no_headings(self):
        assert _extract_sections_from_markdown("Just some plain text\nwith no headings.") == []

    def test_all_six_levels(self):
        md_text = "\n".join(f"{'#' * i} Level {i}" for i in range(1, 7))
        sections = _extract_sections_from_markdown(md_text)
        assert len(sections) == 6
        assert [s["level"] for s in sections] == [1, 2, 3, 4, 5, 6]

    def test_nbsp_normalized_to_space(self):
        """Non-breaking spaces in headings should become regular spaces."""
        md_text = "## Vol.\u00a0II\n\nContent."
        sections = _extract_sections_from_markdown(md_text)
        assert sections[0]["name"] == "Vol. II"

    def test_exotic_whitespace_normalized(self):
        """Em spaces, thin spaces, etc. should become regular spaces."""
        md_text = "## Section\u2003Name\n\nContent."  # \u2003 = em space
        sections = _extract_sections_from_markdown(md_text)
        assert sections[0]["name"] == "Section Name"

    def test_skips_headings_inside_fenced_code_blocks(self):
        """Lines starting with # inside ``` blocks are comments, not headings."""
        md_text = (
            "## Real Heading\n\n"
            "Some text.\n\n"
            "```python\n"
            "# This is a comment\n"
            "## This is also a comment\n"
            "def foo():\n"
            "    pass\n"
            "```\n\n"
            "## Another Real Heading\n\n"
            "More text."
        )
        sections = _extract_sections_from_markdown(md_text)
        names = [s["name"] for s in sections]
        assert names == ["Real Heading", "Another Real Heading"]

    def test_skips_single_char_headings(self):
        """Single-character 'headings' are noise from LaTeX rendering artifacts."""
        md_text = "## Real Heading\n\nContent.\n\n#\n(\n\n## Another Real\n\nMore."
        sections = _extract_sections_from_markdown(md_text)
        names = [s["name"] for s in sections]
        assert "(" not in names
        assert names == ["Real Heading", "Another Real"]

    def test_skips_headings_inside_tilde_fenced_blocks(self):
        """~~~ fences should also be recognized."""
        md_text = "## Before\n\n~~~\n# Not a heading\n~~~\n\n## After\n\nText."
        sections = _extract_sections_from_markdown(md_text)
        names = [s["name"] for s in sections]
        assert names == ["Before", "After"]

    def test_header_only_false_when_body_present(self):
        """Sections with body text should not be marked header-only."""
        sections = _extract_sections_from_markdown(SAMPLE_MARKDOWN)
        for sec in sections:
            assert sec["header_only"] is False, f"{sec['name']} should not be header-only"

    def test_header_only_true_when_no_body(self):
        """A heading followed immediately by a child heading is header-only."""
        md_text = "# Parent\n\n## Child\n\nChild content."
        sections = _extract_sections_from_markdown(md_text)
        assert sections[0]["header_only"] is True
        assert sections[1]["header_only"] is False

    def test_header_only_whitespace_only_body(self):
        """A heading followed by only blank lines before the next heading is header-only."""
        md_text = "## Alpha\n\n\n\n## Beta\n\nReal content."
        sections = _extract_sections_from_markdown(md_text)
        assert sections[0]["header_only"] is True
        assert sections[1]["header_only"] is False

    def test_header_only_last_section_at_eof(self):
        """A final heading with no trailing content is header-only."""
        md_text = "## First\n\nContent.\n\n## Last\n"
        sections = _extract_sections_from_markdown(md_text)
        assert sections[0]["header_only"] is False
        assert sections[1]["header_only"] is True


class TestSlugify:
    def test_basic_heading(self):
        assert _slugify("Section One") == "section-one"

    def test_numbered_heading(self):
        assert _slugify("4. Native INT4 Quantization") == "4-native-int4-quantization"

    def test_special_characters(self):
        assert _slugify("What's New?") == "what-s-new"

    def test_leading_trailing_hyphens_stripped(self):
        assert _slugify("...Introduction...") == "introduction"

    def test_consecutive_non_alnum_collapsed(self):
        assert _slugify("A -- B") == "a-b"

    def test_empty_string(self):
        assert _slugify("") == ""

    def test_unicode_preserved_as_lowercase(self):
        # Non-ASCII alphanumerics are stripped by the regex (only a-z0-9 kept)
        assert _slugify("Café") == "caf"


class TestCleanHeadings:
    """Test heading cleanup via html_to_markdown (which calls _clean_headings)."""

    def test_strips_bold_from_heading(self):
        html = "<html><body><h2><strong>Bold Section</strong></h2><p>Content.</p></body></html>"
        _, markdown = html_to_markdown(html)
        sections = _extract_sections_from_markdown(markdown)
        assert sections[0]["name"] == "Bold Section"

    def test_strips_italic_from_heading(self):
        html = "<html><body><h2><i>Italic Title</i></h2><p>Content.</p></body></html>"
        _, markdown = html_to_markdown(html)
        sections = _extract_sections_from_markdown(markdown)
        assert sections[0]["name"] == "Italic Title"

    def test_strips_link_from_heading(self):
        html = '<html><body><h2><a href="https://example.com">Linked Title</a></h2><p>Content.</p></body></html>'
        _, markdown = html_to_markdown(html)
        sections = _extract_sections_from_markdown(markdown)
        assert sections[0]["name"] == "Linked Title"

    def test_strips_mixed_inline_from_heading(self):
        """Heading with link + text should produce clean combined text."""
        html = '<html><body><h2>Leave a Reply <a href="/cancel">Cancel reply</a></h2><p>Content.</p></body></html>'
        _, markdown = html_to_markdown(html)
        sections = _extract_sections_from_markdown(markdown)
        assert sections[0]["name"] == "Leave a Reply Cancel reply"

    def test_preserves_body_links(self):
        """Links in body content should not be stripped."""
        html = '<html><body><h2>Title</h2><p>See <a href="https://example.com">this link</a>.</p></body></html>'
        _, markdown = html_to_markdown(html)
        assert "https://example.com" in markdown

    def test_strips_empty_text_permalink_anchor(self):
        """Self-link permalink anchors with empty text must not leak into names.

        Spec documents like the WHATWG HTML Living Standard append an
        empty-text ``<a href="#slug" class="self-link"></a>`` to every
        heading as a permalink widget.  htmd renders this as the markdown
        link ``[](#slug)``.  Without stripping, section names become e.g.
        ``"1 Introduction[](#introduction)"`` and callers who type the
        human-visible heading (``"1 Introduction"``) fail to match.
        """
        html = (
            '<html><body>'
            '<h2>1 Introduction<a href="#introduction" class="self-link"></a></h2>'
            '<p>Content.</p>'
            '</body></html>'
        )
        _, markdown = html_to_markdown(html)
        sections = _extract_sections_from_markdown(markdown)
        assert sections[0]["name"] == "1 Introduction"


class TestHtmlTitleExtraction:
    """Title resolution ladder inside html_to_markdown."""

    def test_real_h1_outside_code_wins(self):
        """When a real h1 is present, it's preferred over <title>."""
        html = (
            '<html><head><title>Head Title</title></head>'
            '<body><h1>Real Heading</h1><p>body.</p></body></html>'
        )
        title, _ = html_to_markdown(html)
        assert title == "Real Heading"

    def test_shell_comment_in_code_block_does_not_become_title(self):
        """ATX-looking lines inside fenced code blocks must not be captured.

        Regression: shell/Python/Make comments in example code blocks
        begin with ``# `` and used to be matched as a document-level h1
        because the title regex searched the full markdown without
        respecting fence boundaries.  Concretely, this broke the WHATWG
        HTML Living Standard — WHATWG's real h1 lives inside ``<header>``
        (which htmd decomposes), and the first surviving ``# `` line is
        a bash comment inside a ``<textarea>`` example.
        """
        html = (
            '<html><head><title>Real Page Title</title></head>'
            '<body>'
            '<p>Intro paragraph.</p>'
            '<pre><code># This is a shell comment in a code block\n'
            '# Another comment line\n'
            'echo hello</code></pre>'
            '<p>After code.</p>'
            '</body></html>'
        )
        title, _ = html_to_markdown(html)
        assert title == "Real Page Title"

    def test_whatwg_fixture_title_falls_back_to_title_tag(self):
        """End-to-end against the real WHATWG fixture.

        WHATWG's ``<h1 class="allcaps">HTML</h1>`` is nested inside
        ``<header><hgroup>``, which htmd's ``skip_tags`` decomposes as
        site-chrome noise (the correct default for 99 % of the open
        web, but too aggressive for spec docs).  With no surviving h1,
        the title ladder falls through to ``<title>HTML Standard</title>``.
        Before this fix the first ``# `` line in the rendered markdown
        was a bash comment from a code example — ``"System-wide .bashrc
        file for interactive bash(1) shells."`` — and that became the
        document title.
        """
        import gzip
        from pathlib import Path

        fixture = (
            Path(__file__).parent / "fixtures" / "perf" / "whatwg_html.html.gz"
        )
        if not fixture.exists():
            import pytest
            pytest.skip(f"Fixture {fixture} not present")
        with gzip.open(fixture, "rt", encoding="utf-8") as f:
            whatwg_html = f.read()

        title, _ = html_to_markdown(whatwg_html)
        assert title == "HTML Standard"


# --- _build_section_list ---

class TestBuildSectionList:
    def test_basic_indentation(self):
        sections = _extract_sections_from_markdown(SAMPLE_MARKDOWN)
        lines = _build_section_list(sections)
        assert lines[0] == "- Main Title"
        assert lines[1] == "  - Section One"
        assert lines[3] == "    - Subsection A"

    def test_duplicate_disambiguation(self):
        sections = _extract_sections_from_markdown(SAMPLE_MARKDOWN_WITH_DUPLICATES)
        lines = _build_section_list(sections)
        # Both "Details" should be disambiguated with parent names
        details_lines = [line for line in lines if "Details" in line]
        assert len(details_lines) == 2
        assert any("(Overview)" in line for line in details_lines)
        assert any("(History)" in line for line in details_lines)

    def test_max_sections_cap(self):
        sections = _extract_sections_from_markdown(SAMPLE_MARKDOWN)
        lines = _build_section_list(sections, max_sections=2)
        assert len(lines) == 3  # 2 sections + overflow message
        assert "... and 3 more sections" in lines[-1]

    def test_empty_sections(self):
        assert _build_section_list([]) == []

    def test_single_section(self):
        sections = _extract_sections_from_markdown("# Only One\n\nContent.")
        lines = _build_section_list(sections)
        assert lines == ["- Only One"]

    def test_header_only_annotation(self):
        """Header-only sections should be annotated in the section list."""
        md_text = "# Parent\n\n## Child One\n\nContent.\n\n## Child Two\n\nMore."
        sections = _extract_sections_from_markdown(md_text)
        lines = _build_section_list(sections)
        assert lines[0] == "- Parent [header only]"
        assert "[header only]" not in lines[1]
        assert "[header only]" not in lines[2]

    def test_header_only_annotation_with_slugs(self):
        """Header-only annotation should appear after the slug."""
        md_text = "# Parent\n\n## Child\n\nContent."
        sections = _extract_sections_from_markdown(md_text)
        lines = _build_section_list(sections, include_slugs=True)
        assert lines[0] == "- Parent (#parent) [header only]"


# --- _filter_markdown_by_sections ---

class TestFilterMarkdownBySections:
    def test_single_section_extraction(self):
        sections = _extract_sections_from_markdown(SAMPLE_MARKDOWN)
        filtered, _meta, _unmatched = _filter_markdown_by_sections(SAMPLE_MARKDOWN, ["Section Two"], sections)
        assert "Content of section two" in filtered
        # Each section is its own entry — subsections are separate entries
        assert "Section One" not in filtered
        assert "Section Three" not in filtered

    def test_multiple_section_extraction(self):
        sections = _extract_sections_from_markdown(SAMPLE_MARKDOWN)
        filtered, meta, _unmatched = _filter_markdown_by_sections(
            SAMPLE_MARKDOWN, ["Section One", "Section Three"], sections
        )
        assert "Content of section one" in filtered
        assert "More content here" in filtered
        assert len(meta) == 2

    def test_ancestry_path_toplevel(self):
        sections = _extract_sections_from_markdown(SAMPLE_MARKDOWN)
        _, meta, _ = _filter_markdown_by_sections(SAMPLE_MARKDOWN, ["Section One"], sections)
        assert meta[0]["ancestry_path"] == "Main Title > Section One"

    def test_ancestry_path_nested(self):
        sections = _extract_sections_from_markdown(SAMPLE_MARKDOWN)
        _, meta, _ = _filter_markdown_by_sections(SAMPLE_MARKDOWN, ["Subsection A"], sections)
        assert meta[0]["ancestry_path"] == "Main Title > Section Two > Subsection A"

    def test_has_subsections_true(self):
        """Parent sections with children should be flagged."""
        sections = _extract_sections_from_markdown(SAMPLE_MARKDOWN)
        _, meta, _ = _filter_markdown_by_sections(SAMPLE_MARKDOWN, ["Section Two"], sections)
        assert meta[0].get("has_subsections") is True

    def test_has_subsections_false(self):
        """Leaf sections should not have the has_subsections flag."""
        sections = _extract_sections_from_markdown(SAMPLE_MARKDOWN)
        _, meta, _ = _filter_markdown_by_sections(SAMPLE_MARKDOWN, ["Section One"], sections)
        assert "has_subsections" not in meta[0]

    def test_has_subsections_via_slug(self):
        """has_subsections should work through slug and fuzzy match paths too."""
        sections = _extract_sections_from_markdown(SAMPLE_MARKDOWN)
        _, meta, _ = _filter_markdown_by_sections(SAMPLE_MARKDOWN, ["section-two"], sections)
        assert meta[0].get("has_subsections") is True

    def test_unmatched_section_returned(self):
        sections = _extract_sections_from_markdown(SAMPLE_MARKDOWN)
        filtered, meta, unmatched = _filter_markdown_by_sections(SAMPLE_MARKDOWN, ["Nonexistent"], sections)
        assert unmatched == ["Nonexistent"]
        assert meta == []
        assert filtered == ""

    def test_mixed_matched_and_unmatched(self):
        sections = _extract_sections_from_markdown(SAMPLE_MARKDOWN)
        filtered, meta, unmatched = _filter_markdown_by_sections(
            SAMPLE_MARKDOWN, ["Section One", "Nonexistent"], sections
        )
        assert "Content of section one" in filtered
        assert unmatched == ["Nonexistent"]
        assert len(meta) == 1

    def test_disambiguated_name_match(self):
        sections = _extract_sections_from_markdown(SAMPLE_MARKDOWN_WITH_DUPLICATES)
        filtered, _meta, unmatched = _filter_markdown_by_sections(
            SAMPLE_MARKDOWN_WITH_DUPLICATES, ["Details (Overview)"], sections
        )
        assert "First details" in filtered
        assert "Second details" not in filtered
        assert unmatched == []

    def test_all_sections_empty(self):
        filtered, _meta, unmatched = _filter_markdown_by_sections("No headings here.", ["Foo"], [])
        assert unmatched == ["Foo"]
        assert filtered == ""

    def test_nbsp_in_heading_matches_regular_space_request(self):
        """Requesting 'Vol. II' (regular space) matches heading with nbsp."""
        md_text = "## Vol.\u00a0II\n\nNbsp content here.\n\n## Other\n\nOther content."
        sections = _extract_sections_from_markdown(md_text)
        filtered, meta, unmatched = _filter_markdown_by_sections(
            md_text, ["Vol. II"], sections
        )
        assert "Nbsp content here" in filtered
        assert unmatched == []
        assert meta[0]["name"] == "Vol. II"

    def test_slug_match_fallback(self):
        """URL fragment slug should match heading text when exact match fails."""
        sections = _extract_sections_from_markdown(SAMPLE_MARKDOWN)
        filtered, meta, unmatched = _filter_markdown_by_sections(
            SAMPLE_MARKDOWN, ["section-two"], sections
        )
        assert "Content of section two" in filtered
        assert unmatched == []
        assert meta[0]["name"] == "Section Two"
        assert meta[0]["matched_fragment"] == "section-two"

    def test_slug_match_numbered_heading(self):
        """Numbered heading like '4. Native INT4 Quantization' matches its slug."""
        md_text = "## 4. Native INT4 Quantization\n\nQuantization content.\n\n## Other\n\nOther."
        sections = _extract_sections_from_markdown(md_text)
        filtered, meta, unmatched = _filter_markdown_by_sections(
            md_text, ["4-native-int4-quantization"], sections
        )
        assert "Quantization content" in filtered
        assert unmatched == []
        assert meta[0]["name"] == "4. Native INT4 Quantization"
        assert meta[0]["matched_fragment"] == "4-native-int4-quantization"

    def test_exact_match_takes_precedence_over_slug(self):
        """Exact name match should not produce matched_fragment metadata."""
        sections = _extract_sections_from_markdown(SAMPLE_MARKDOWN)
        _, meta, unmatched = _filter_markdown_by_sections(
            SAMPLE_MARKDOWN, ["Section Two"], sections
        )
        assert unmatched == []
        assert "matched_fragment" not in meta[0]

    def test_slug_no_match_returns_unmatched(self):
        """A slug that doesn't match any heading should appear in unmatched."""
        sections = _extract_sections_from_markdown(SAMPLE_MARKDOWN)
        _, meta, unmatched = _filter_markdown_by_sections(
            SAMPLE_MARKDOWN, ["nonexistent-section"], sections
        )
        assert unmatched == ["nonexistent-section"]
        assert meta == []

    def test_fuzzy_underscore_to_hyphen(self):
        """GFM-style fragment with underscores matches Goldmark-style slug."""
        md_text = "## What is this DQ3_K_M?\n\nContent here.\n\n## Other\n\nOther."
        sections = _extract_sections_from_markdown(md_text)
        filtered, meta, unmatched = _filter_markdown_by_sections(
            md_text, ["what-is-this-dq3_k_m"], sections
        )
        assert "Content here" in filtered
        assert unmatched == []
        assert meta[0]["name"] == "What is this DQ3_K_M?"
        assert meta[0]["matched_fragment"] == "what-is-this-dq3_k_m"

    def test_fuzzy_case_folding(self):
        """Mixed-case Wikipedia fragment resolves via case-folding."""
        md_text = "## Sparsely-gated MoE layer\n\nMoE content.\n\n## Other\n\nOther."
        sections = _extract_sections_from_markdown(md_text)
        filtered, meta, unmatched = _filter_markdown_by_sections(
            md_text, ["Sparsely-gated_MoE_layer"], sections
        )
        assert "MoE content" in filtered
        assert unmatched == []
        assert meta[0]["matched_fragment"] == "Sparsely-gated_MoE_layer"

    def test_fuzzy_percent_encoded_apostrophe(self):
        """Percent-encoded apostrophe in fragment resolves correctly."""
        md_text = "## The Hitchhiker's Guide\n\nDon't panic.\n\n## Other\n\nOther."
        sections = _extract_sections_from_markdown(md_text)
        filtered, meta, unmatched = _filter_markdown_by_sections(
            md_text, ["The_Hitchhiker%27s_Guide"], sections
        )
        assert "Don't panic" in filtered
        assert unmatched == []
        assert meta[0]["name"] == "The Hitchhiker's Guide"

    def test_fuzzy_combined_case_underscore_apostrophe(self):
        """Combined case, underscore, and apostrophe mismatch resolves."""
        md_text = "## The Author's Notes\n\nNotes content.\n\n## Other\n\nOther."
        sections = _extract_sections_from_markdown(md_text)
        filtered, _meta, unmatched = _filter_markdown_by_sections(
            md_text, ["The_Author%27s_Notes"], sections
        )
        assert "Notes content" in filtered
        assert unmatched == []

    def test_fuzzy_no_false_positive(self):
        """Fuzzy fallback should not match when slugs are genuinely different."""
        md_text = "## Alpha Beta\n\nContent.\n\n## Other\n\nOther."
        sections = _extract_sections_from_markdown(md_text)
        _, meta, unmatched = _filter_markdown_by_sections(
            md_text, ["gamma-delta"], sections
        )
        assert unmatched == ["gamma-delta"]
        assert meta == []


# --- _build_section_list with slugs ---

class TestBuildSectionListWithSlugs:
    def test_include_slugs(self):
        sections = _extract_sections_from_markdown(SAMPLE_MARKDOWN)
        lines = _build_section_list(sections, include_slugs=True)
        assert lines[0] == "- Main Title (#main-title)"
        assert lines[1] == "  - Section One (#section-one)"

    def test_slugs_off_by_default(self):
        sections = _extract_sections_from_markdown(SAMPLE_MARKDOWN)
        lines = _build_section_list(sections)
        assert "(#" not in lines[0]


# --- _build_frontmatter ---

class TestBuildFrontmatter:
    def test_basic_entries(self):
        fm = _build_frontmatter({"title": "Test", "source": "http://example.com"})
        assert fm.startswith("---")
        assert fm.endswith("---")
        assert "title: Test" in fm
        assert "source: http://example.com" in fm

    def test_skips_none_values(self):
        fm = _build_frontmatter({"title": "Test", "truncated": None})
        assert "truncated" not in fm

    def test_sections_not_found(self):
        fm = _build_frontmatter({"title": "T"}, sections_not_found=["Missing", "Also Missing"])
        assert "sections_not_found:" in fm
        assert '  - "Missing"' in fm
        assert '  - "Also Missing"' in fm

    def test_sections_not_found_with_commas(self):
        """Names containing commas must not be ambiguous in YAML output."""
        fm = _build_frontmatter({"title": "T"}, sections_not_found=["Parables, Vol. I", "Parables, Vol. II"])
        assert '  - "Parables, Vol. I"' in fm
        assert '  - "Parables, Vol. II"' in fm

    def test_sections_not_found_none_omitted(self):
        fm = _build_frontmatter({"title": "T"}, sections_not_found=None)
        assert "sections_not_found" not in fm

    def test_list_value_single_item(self):
        """Single-item list should render as a scalar."""
        fm = _build_frontmatter({"title": "T", "warning": ["one warning"]})
        assert "warning: one warning" in fm
        assert "  -" not in fm.split("sections")[0]  # no list markers

    def test_list_value_multiple_items(self):
        """Multi-item list should render as a YAML list."""
        fm = _build_frontmatter({"title": "T", "warning": ["first", "second"]})
        assert "warning:" in fm
        assert "  - first" in fm
        assert "  - second" in fm

    def test_no_sections(self):
        fm = _build_frontmatter({"title": "T"})
        assert "sections:" not in fm
        assert "section:" not in fm


class TestApplyHardTruncation:
    def test_short_content_unchanged(self):
        content = "Short text."
        result, hint = _apply_hard_truncation(content, 100)
        assert result == content
        assert hint is None

    def test_long_content_truncated(self):
        content = "x" * 5000
        result, hint = _apply_hard_truncation(content, 100)
        assert len(result) < len(content)
        assert hint is not None
        assert "tokens" in hint


class TestApplySemanticTruncation:
    def test_short_content_unchanged(self):
        content = "Short markdown text."
        result, hint = _apply_semantic_truncation(content, 100)
        assert result == content
        assert hint is None

    def test_long_markdown_truncates(self):
        """Semantic truncation should produce truncated output with marker and hint."""
        sections = []
        for i in range(20):
            words = " ".join(f"word{j}" for j in range(100))
            sections.append(f"## Section {i}\n\n{words}.")
        content = "\n\n".join(sections)

        result, hint = _apply_semantic_truncation(content, 200)
        assert hint is not None
        assert "tokens" in hint
        # Output should be shorter than the original
        assert len(result) < len(content)

    def test_hint_includes_actual_shown_tokens(self):
        """Hint should reflect the actual content shown, not max_tokens."""
        paragraphs = [f"## Section {i}\n\n{'Word ' * 200}" for i in range(20)]
        content = "\n\n".join(paragraphs)

        result, hint = _apply_semantic_truncation(content, 500)
        assert hint is not None
        # The hint should mention the actual shown amount
        assert "showing first" in hint
        assert "Full page is" in hint

    # --- Regression tests for issue #5 ---
    #
    # MarkdownSplitter treats headings as the highest-priority semantic
    # boundary.  For "## Heading\n\n<large body>" where the body exceeds
    # char_limit, it emits chunk 0 = "## Heading" alone and the body in
    # subsequent chunks.  The old implementation returned only chunks[0],
    # destroying the body.  These tests pin the pack-chunks behavior.

    def test_heading_plus_oversized_table_packs_body(self):
        """Heading followed by an oversized markdown table must pack body rows.

        Regression for issue #5: ``section="Film"`` on Wikipedia filmography
        pages previously returned just the ``## Film`` heading line.
        """
        rows = "\n".join(
            f"| {y} | Title {y} | Role {y} | Notes {y} |"
            for y in range(1967, 2100)
        )
        content = (
            "## Film\n\n"
            "| Year | Title | Role | Notes |\n"
            "| --- | --- | --- | --- |\n"
            f"{rows}\n"
        )
        # Budget large enough for the heading plus several rows but not all.
        result, hint = _apply_semantic_truncation(content, max_tokens=200)

        assert hint is not None
        assert "## Film" in result
        # The body must be present — at least the first few rows.
        assert "| 1967 | Title 1967 |" in result
        assert "| 1968 | Title 1968 |" in result
        # And the result must be meaningfully larger than just the heading.
        assert len(result) > 100

    def test_heading_plus_oversized_paragraph_packs_body(self):
        """Same shape but with a paragraph body instead of a table."""
        sentences = " ".join(
            f"This is sentence number {i} in a long paragraph."
            for i in range(500)
        )
        content = f"## Overview\n\n{sentences}\n"

        result, hint = _apply_semantic_truncation(content, max_tokens=200)

        assert hint is not None
        assert "## Overview" in result
        assert "This is sentence number 0" in result
        assert len(result) > 100

    def test_multi_section_packing_preserves_body(self):
        """Multi-section content whose first section overflows the budget
        must still return body content for that section, not just a heading.

        Tests the soft-overflow guard: when the heading becomes its own
        tiny chunk, the packer must include the following body chunk even
        if it pushes slightly over char_limit.
        """
        body_a = " ".join(f"alpha{i}" for i in range(200))
        body_b = " ".join(f"beta{i}" for i in range(200))
        content = f"## Section A\n\n{body_a}\n\n## Section B\n\n{body_b}\n"

        # char_limit=1200 (max_tokens=300) is smaller than section A alone,
        # forcing the splitter to cleave "## Section A" into its own chunk.
        result, hint = _apply_semantic_truncation(content, max_tokens=300)

        assert hint is not None
        assert "## Section A" in result
        assert "alpha0" in result, (
            "body content must be packed alongside the heading"
        )
        # Output must be meaningfully larger than a bare heading line.
        assert len(result) > 500

    def test_pathological_single_oversized_chunk_returned(self):
        """If chunk 0 already exceeds char_limit, return it anyway.

        The MarkdownSplitter may emit an atomic chunk (e.g. a single giant
        table row) larger than char_limit.  The function must not return
        an empty string or loop forever — it should emit the oversized
        chunk with the truncation hint.
        """
        # A single markdown table row with an enormous cell.
        giant_cell = "x" * 20000
        content = f"| col |\n| --- |\n| {giant_cell} |\n"

        result, hint = _apply_semantic_truncation(content, max_tokens=100)

        assert hint is not None
        assert len(result) > 0
        # The oversized chunk still contains recognizable table structure.
        assert "col" in result or "x" in result

    def test_hint_shown_tokens_matches_truncated_length(self):
        """The hint's 'showing first ~N tokens' must reflect len(truncated)//4."""
        rows = "\n".join(
            f"| {i} | data {i} | more {i} |" for i in range(500)
        )
        content = f"## Data\n\n| A | B | C |\n| - | - | - |\n{rows}\n"

        result, hint = _apply_semantic_truncation(content, max_tokens=300)

        assert hint is not None
        expected_tokens = len(result) // 4
        # Hint format: "showing first ~{N:,} tokens"
        assert f"showing first ~{expected_tokens:,} tokens" in hint

    def test_wikipedia_filmography_fixture_regression(self):
        """End-to-end regression using a captured Wikipedia filmography page.

        Runs the full ``_extract_sections_from_markdown`` +
        ``_filter_markdown_by_sections`` + ``_apply_semantic_truncation``
        pipeline against a real rendered MediaWiki page and asserts that
        requesting ``section="Film"`` returns actual filmography rows, not
        just the heading line.  Network-free — fixture is checked in.
        """
        from pathlib import Path

        fixture_path = (
            Path(__file__).parent / "fixtures" / "malcolm_mcdowell_filmography.md"
        )
        content = fixture_path.read_text()

        sections = _extract_sections_from_markdown(content)
        filtered, matched, unmatched = _filter_markdown_by_sections(
            content, ["Film"], sections
        )
        assert unmatched == []
        assert matched, "Film section should have matched"

        # Apply the same budget the default pipeline uses for section fetches.
        truncated, hint = _apply_semantic_truncation(filtered, max_tokens=3000)

        assert hint is not None, "Film section should overflow default budget"
        assert "## Film" in truncated
        # Recognizable entries from the real page.  These pin the regression.
        assert "A Clockwork Orange" in truncated
        assert "1971" in truncated


class TestFenceContent:
    def test_basic_fencing(self):
        result = _fence_content("Hello world")
        assert result.startswith(_FENCE_OPEN)
        assert result.endswith(_FENCE_CLOSE)
        assert "│ Hello world" in result

    def test_with_title(self):
        result = _fence_content("Body text", title="Page Title")
        lines = result.split("\n")
        assert lines[0] == _FENCE_OPEN
        assert lines[1] == "│"  # separator after open fence
        assert lines[2] == "│ # Page Title"
        assert lines[3] == "│ "
        assert lines[4] == "│ Body text"
        assert lines[-2] == "│"  # separator before close fence
        assert lines[-1] == _FENCE_CLOSE

    def test_multiline_content(self):
        result = _fence_content("Line 1\nLine 2\nLine 3")
        assert "│ Line 1" in result
        assert "│ Line 2" in result
        assert "│ Line 3" in result

    def test_no_title(self):
        result = _fence_content("Content only")
        lines = result.split("\n")
        assert lines[1] == "│"  # separator after open fence
        assert "│ Content only" in result

    def test_empty_content_with_title(self):
        result = _fence_content("", title="Just a Title")
        assert "│ # Just a Title" in result
        assert result.startswith(_FENCE_OPEN)
        assert result.endswith(_FENCE_CLOSE)

    def test_self_labeling_delimiters(self):
        """Fence delimiters should carry semantic meaning without external explanation."""
        assert "untrusted" in _FENCE_OPEN
        assert "untrusted" in _FENCE_CLOSE


# --- Retraction banner ---


class TestFormatRetractionBanner:
    def test_retraction_full(self):
        banner = _format_retraction_banner(
            {
                "notice_doi": "10.1016/s0140-6736(20)31324-6",
                "date": "2020-06-05",
                "source": "retraction-watch",
                "label": "Retraction",
            },
            None,
        )
        assert banner is not None
        assert "[RETRACTED]" in banner
        assert "retracted" in banner
        assert "2020-06-05" in banner
        assert "10.1016/s0140-6736(20)31324-6" in banner
        assert "retraction-watch" in banner

    def test_expression_of_concern(self):
        banner = _format_retraction_banner(
            None,
            {
                "type": "expression_of_concern",
                "notice_doi": "10.1234/concerned",
                "date": "2021-03-15",
                "source": "publisher",
                "label": None,
            },
        )
        assert banner is not None
        assert "[EXPRESSION OF CONCERN]" in banner
        assert "called into question" in banner

    def test_correction(self):
        banner = _format_retraction_banner(
            None,
            {
                "type": "correction",
                "notice_doi": "10.1234/corr",
                "date": "2022-01-01",
                "source": "publisher",
                "label": None,
            },
        )
        assert banner is not None
        assert "[CORRECTED]" in banner

    def test_none_inputs(self):
        assert _format_retraction_banner(None, None) is None

    def test_retraction_beats_other_update(self):
        """If both are passed, retraction takes precedence."""
        banner = _format_retraction_banner(
            {"notice_doi": "10.1234/ret", "date": "2020", "source": "publisher", "label": None},
            {"type": "correction", "notice_doi": "10.1234/corr", "date": "2021", "source": "publisher"},
        )
        assert banner is not None
        assert "[RETRACTED]" in banner
        assert "[CORRECTED]" not in banner

    def test_minimal_entry(self):
        """Banner renders even when optional fields are absent."""
        banner = _format_retraction_banner(
            {"notice_doi": None, "date": None, "source": "unknown", "label": None},
            None,
        )
        assert banner is not None
        assert "[RETRACTED]" in banner

    def test_label_rendered_inline(self):
        banner = _format_retraction_banner(
            {
                "notice_doi": "10.1234/ret",
                "date": "2020-06-05",
                "source": "publisher",
                "label": "Retraction of Vaswani et al.",
            },
            None,
        )
        assert banner is not None
        assert "Retraction of Vaswani" in banner
