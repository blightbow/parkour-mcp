"""Tests for content slicing, keyword search, and slice retrieval."""

import httpx
import pytest
import respx

from kagi_research_mcp.fetch_direct import web_fetch_direct
from kagi_research_mcp.markdown import (
    _compute_slice_ancestry,
    _extract_sections_from_markdown,
)
from kagi_research_mcp._pipeline import _page_cache, _wiki_cache


@pytest.fixture(autouse=True)
def clear_caches():
    """Ensure each test starts with empty caches."""
    yield
    _wiki_cache.url = None
    _wiki_cache.wiki_info = None
    _wiki_cache.wiki_page = None
    _page_cache.url = None
    _page_cache.title = None
    _page_cache.markdown = None
    _page_cache.slices = None
    _page_cache.slice_ancestry = None
    _page_cache.renderer = None


# ---------------------------------------------------------------------------
# Sample HTML for slicing tests — large enough to produce multiple slices
# ---------------------------------------------------------------------------

def _build_large_html(num_sections=10, paragraphs_per_section=3):
    """Build an HTML page large enough to generate multiple slices."""
    parts = [
        "<html><head><title>Slicing Test</title></head><body>",
        "<h1>Slicing Test</h1>",
        "<p>Introduction paragraph with some content.</p>",
    ]
    for i in range(1, num_sections + 1):
        parts.append(f"<h2>Section {i}</h2>")
        for j in range(1, paragraphs_per_section + 1):
            parts.append(
                f"<p>Paragraph {j} of section {i}. "
                "This is filler text to ensure each section has enough content "
                "to be meaningful for slicing. The quick brown fox jumps over "
                "the lazy dog. Additional text to pad out the paragraph and "
                "make it realistic in length for testing purposes.</p>"
            )
    parts.append("</body></html>")
    return "\n".join(parts)


LARGE_HTML = _build_large_html()

# Smaller HTML with a keyword in a specific section
SEARCHABLE_HTML = """\
<html><head><title>Searchable Page</title></head><body>
<h1>Searchable Page</h1>
<p>This is the introduction. Nothing special here.</p>
<h2>Background</h2>
<p>Background information about the topic. General context provided here
with enough text to fill a reasonable chunk for testing purposes.</p>
<h2>Methodology</h2>
<p>We used the frobnicate algorithm to process the data. This is the key
finding that someone might search for. The frobnicate approach was chosen
because of its superior performance characteristics.</p>
<h2>Results</h2>
<p>The results show clear improvements. Statistical significance was
achieved across all metrics with p-values below the threshold.</p>
<h2>Discussion</h2>
<p>The frobnicate method proved effective in this context. We discuss
implications and future directions for research in this area.</p>
</body></html>
"""


# ---------------------------------------------------------------------------
# _compute_slice_ancestry unit tests
# ---------------------------------------------------------------------------

class TestComputeSliceAncestry:
    def test_no_sections_returns_empty_strings(self):
        result = _compute_slice_ancestry([], [0, 100, 200])
        assert result == ["", "", ""]

    def test_no_offsets_returns_empty_list(self):
        sections = [{"name": "Intro", "level": 2, "start_pos": 0, "end_pos": 100}]
        result = _compute_slice_ancestry(sections, [])
        assert result == []

    def test_single_section_single_slice(self):
        sections = [{"name": "Intro", "level": 2, "start_pos": 0, "end_pos": 500}]
        result = _compute_slice_ancestry(sections, [10])
        assert result == ["Intro"]

    def test_nested_sections(self):
        sections = [
            {"name": "Main", "level": 1, "start_pos": 0, "end_pos": 500},
            {"name": "Sub", "level": 2, "start_pos": 100, "end_pos": 500},
        ]
        result = _compute_slice_ancestry(sections, [150])
        assert result == ["Main > Sub"]

    def test_chunk_before_first_heading(self):
        sections = [
            {"name": "First", "level": 2, "start_pos": 200, "end_pos": 500},
        ]
        result = _compute_slice_ancestry(sections, [50])
        assert result == [""]

    def test_multi_slice_section_gets_positional_hint(self):
        sections = [
            {"name": "Long Section", "level": 2, "start_pos": 0, "end_pos": 1000},
        ]
        # Three consecutive chunks all within the same section
        result = _compute_slice_ancestry(sections, [0, 300, 600])
        assert result == [
            "Long Section (1/3)",
            "Long Section (2/3)",
            "Long Section (3/3)",
        ]

    def test_single_slice_section_no_hint(self):
        sections = [
            {"name": "A", "level": 2, "start_pos": 0, "end_pos": 100},
            {"name": "B", "level": 2, "start_pos": 100, "end_pos": 200},
        ]
        result = _compute_slice_ancestry(sections, [10, 110])
        assert result == ["A", "B"]

    def test_mixed_single_and_multi_slice(self):
        sections = [
            {"name": "Short", "level": 2, "start_pos": 0, "end_pos": 100},
            {"name": "Long", "level": 2, "start_pos": 100, "end_pos": 500},
        ]
        # 1 chunk in Short, 2 consecutive in Long
        result = _compute_slice_ancestry(sections, [10, 150, 350])
        assert result == ["Short", "Long (1/2)", "Long (2/2)"]

    def test_deep_nesting_with_positional_hint(self):
        sections = [
            {"name": "Top", "level": 1, "start_pos": 0, "end_pos": 1000},
            {"name": "Mid", "level": 2, "start_pos": 50, "end_pos": 1000},
            {"name": "Deep", "level": 3, "start_pos": 100, "end_pos": 1000},
        ]
        result = _compute_slice_ancestry(sections, [200, 500])
        assert result == [
            "Top > Mid > Deep (1/2)",
            "Top > Mid > Deep (2/2)",
        ]


# ---------------------------------------------------------------------------
# _PageCache unit tests
# ---------------------------------------------------------------------------

class TestPageCache:
    def test_store_and_get(self):
        md = "# Title\n\nSome content here.\n\n## Section\n\nMore content."
        _page_cache.store("https://example.com", "Title", md)
        cached = _page_cache.get("https://example.com")
        assert cached is not None
        assert cached.title == "Title"
        assert cached.slices is not None
        assert len(cached.slices) >= 1
        assert cached.slice_ancestry is not None
        assert len(cached.slice_ancestry) == len(cached.slices)

    def test_cache_miss(self):
        _page_cache.store("https://example.com", "Title", "# Content")
        assert _page_cache.get("https://other.com") is None

    def test_eviction_on_new_url(self):
        _page_cache.store("https://first.com", "First", "# First")
        _page_cache.store("https://second.com", "Second", "# Second")
        assert _page_cache.get("https://first.com") is None
        cached = _page_cache.get("https://second.com")
        assert cached is not None
        assert cached.title == "Second"

    def test_slices_cover_content(self):
        md = "# Title\n\nParagraph one.\n\n## Section\n\nParagraph two."
        _page_cache.store("https://example.com", "Title", md)
        cached = _page_cache.get("https://example.com")
        assert cached is not None
        assert cached.slices is not None
        # All content should appear in at least one slice
        combined = " ".join(cached.slices)
        assert "Paragraph one" in combined
        assert "Paragraph two" in combined

    def test_bm25_search_returns_ranked_indices(self):
        md = (
            "# Doc\n\n"
            "## Intro\n\nGeneral introduction with no special terms.\n\n"
            "## Methods\n\nWe used the frobnicate algorithm extensively.\n\n"
            "## Results\n\nThe frobnicate approach yielded excellent results.\n\n"
        )
        _page_cache.store("https://example.com", "Doc", md)
        cached = _page_cache.get("https://example.com")
        assert cached is not None
        results = cached.search("frobnicate")
        assert len(results) >= 1
        # Results should be slice indices within range
        assert cached.slices is not None
        for idx in results:
            assert 0 <= idx < len(cached.slices)

    def test_bm25_search_multi_word_independent_terms(self):
        """BM25 matches terms independently — 'training results' matches
        slices containing either term, ranked by relevance."""
        # Each section must exceed the splitter's min capacity (1600 chars)
        # so they end up in separate slices
        padding = " ".join(["filler"] * 300)
        md = (
            "# Doc\n\n"
            f"## Training\n\nWe trained the model on a large dataset. {padding}\n\n"
            f"## Results\n\nThe results show clear improvements. {padding}\n\n"
            f"## Unrelated\n\nThis section has nothing to do with the query. {padding}\n\n"
        )
        _page_cache.store("https://example.com", "Doc", md)
        cached = _page_cache.get("https://example.com")
        assert cached is not None
        results = cached.search("training results")
        # Both "training" and "results" sections should match
        assert len(results) >= 2

    def test_bm25_search_handles_markdown_punctuation(self):
        """tantivy tokenizer strips markdown punctuation, so searching for
        'code' matches `code` in backticks."""
        md = "# Doc\n\nThis has `code` with backticks and **bold** text.\n\n"
        _page_cache.store("https://example.com", "Doc", md)
        cached = _page_cache.get("https://example.com")
        assert cached is not None
        results = cached.search("code")
        assert len(results) >= 1


# ---------------------------------------------------------------------------
# web_fetch_direct — search parameter
# ---------------------------------------------------------------------------

class TestWebFetchDirectSearch:
    @pytest.mark.asyncio
    @respx.mock
    async def test_search_returns_matching_slices(self):
        respx.get("https://example.com/page").mock(
            return_value=httpx.Response(200, text=SEARCHABLE_HTML,
                                        headers={"content-type": "text/html"})
        )
        result = await web_fetch_direct("https://example.com/page", search="frobnicate")
        assert "---" in result
        assert "search:" in result
        assert "frobnicate" in result
        assert "total_slices:" in result
        assert "matched_slices:" in result
        # The keyword should appear in the returned content
        assert "frobnicate algorithm" in result or "frobnicate method" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_search_no_matches(self):
        respx.get("https://example.com/page").mock(
            return_value=httpx.Response(200, text=SEARCHABLE_HTML,
                                        headers={"content-type": "text/html"})
        )
        result = await web_fetch_direct("https://example.com/page", search="xyznonexistent")
        assert "No matching slices found" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_search_case_insensitive(self):
        """BM25 via tantivy handles case-folding — uppercase query matches lowercase content."""
        respx.get("https://example.com/page").mock(
            return_value=httpx.Response(200, text=SEARCHABLE_HTML,
                                        headers={"content-type": "text/html"})
        )
        result = await web_fetch_direct("https://example.com/page", search="FROBNICATE")
        assert "matched_slices:" in result
        assert "none" not in result.lower().split("matched_slices:")[1].split("\n")[0]

    @pytest.mark.asyncio
    @respx.mock
    async def test_search_cache_hit_avoids_refetch(self):
        """Second search call should use cache, not re-fetch."""
        route = respx.get("https://example.com/page").mock(
            return_value=httpx.Response(200, text=SEARCHABLE_HTML,
                                        headers={"content-type": "text/html"})
        )
        # First call: fetches and populates cache
        await web_fetch_direct("https://example.com/page", search="frobnicate")
        assert route.call_count == 1

        # Second call: should hit cache
        result = await web_fetch_direct("https://example.com/page", search="results")
        assert route.call_count == 1  # No additional fetch
        assert "matched_slices:" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_search_empty_string_falls_through(self):
        """Empty search string should behave like normal fetch."""
        respx.get("https://example.com/page").mock(
            return_value=httpx.Response(200, text=SEARCHABLE_HTML,
                                        headers={"content-type": "text/html"})
        )
        result = await web_fetch_direct("https://example.com/page", search="")
        # Should return normal page content, not slice format
        assert "total_slices:" not in result
        assert "source:" in result
        assert "│ # Searchable Page" in result


# ---------------------------------------------------------------------------
# web_fetch_direct — slices parameter
# ---------------------------------------------------------------------------

class TestWebFetchDirectSlices:
    @pytest.mark.asyncio
    @respx.mock
    async def test_slices_returns_specific_indices(self):
        respx.get("https://example.com/page").mock(
            return_value=httpx.Response(200, text=LARGE_HTML,
                                        headers={"content-type": "text/html"})
        )
        result = await web_fetch_direct("https://example.com/page", slices=[0, 1])
        assert "total_slices:" in result
        assert "hint:" in result
        assert "--- slice 0" in result
        assert "--- slice 1" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_slices_single_int(self):
        respx.get("https://example.com/page").mock(
            return_value=httpx.Response(200, text=LARGE_HTML,
                                        headers={"content-type": "text/html"})
        )
        result = await web_fetch_direct("https://example.com/page", slices=0)
        assert "--- slice 0" in result
        assert "total_slices:" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_slices_out_of_range(self):
        respx.get("https://example.com/page").mock(
            return_value=httpx.Response(200, text=SEARCHABLE_HTML,
                                        headers={"content-type": "text/html"})
        )
        result = await web_fetch_direct("https://example.com/page", slices=[999])
        assert "No valid slice indices" in result or "slices_not_found" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_slices_cache_hit(self):
        route = respx.get("https://example.com/page").mock(
            return_value=httpx.Response(200, text=LARGE_HTML,
                                        headers={"content-type": "text/html"})
        )
        # First: normal fetch populates cache
        await web_fetch_direct("https://example.com/page")
        assert route.call_count == 1

        # Second: slices from cache
        result = await web_fetch_direct("https://example.com/page", slices=[0])
        assert route.call_count == 1
        assert "--- slice 0" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_slices_empty_list_falls_through(self):
        respx.get("https://example.com/page").mock(
            return_value=httpx.Response(200, text=SEARCHABLE_HTML,
                                        headers={"content-type": "text/html"})
        )
        result = await web_fetch_direct("https://example.com/page", slices=[])
        assert "total_slices:" not in result


# ---------------------------------------------------------------------------
# Parameter validation
# ---------------------------------------------------------------------------

class TestSlicingValidation:
    @pytest.mark.asyncio
    async def test_search_and_slices_mutually_exclusive(self):
        result = await web_fetch_direct(
            "https://example.com", search="foo", slices=[0]
        )
        assert "mutually exclusive" in result

    @pytest.mark.asyncio
    async def test_search_and_section_mutually_exclusive(self):
        result = await web_fetch_direct(
            "https://example.com", search="foo", section="Bar"
        )
        assert "mutually exclusive" in result

    @pytest.mark.asyncio
    async def test_slices_and_section_mutually_exclusive(self):
        result = await web_fetch_direct(
            "https://example.com", slices=[0], section="Bar"
        )
        assert "mutually exclusive" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_search_with_footnotes_warns(self):
        """search + footnotes should honor search and warn about footnotes."""
        respx.get("https://example.com").mock(
            return_value=httpx.Response(
                200, text="<html><body><p>Content</p></body></html>",
                headers={"content-type": "text/html"},
            )
        )
        result = await web_fetch_direct(
            "https://example.com", search="foo", footnotes=[1]
        )
        assert "footnotes parameter ignored" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_search_on_json_returns_error(self):
        respx.get("https://example.com/data.json").mock(
            return_value=httpx.Response(
                200, text='{"key": "value"}',
                headers={"content-type": "application/json"},
            )
        )
        result = await web_fetch_direct(
            "https://example.com/data.json", search="key"
        )
        assert "requires HTML" in result


# ---------------------------------------------------------------------------
# Slice ancestry in output
# ---------------------------------------------------------------------------

class TestSliceAncestryInOutput:
    @pytest.mark.asyncio
    @respx.mock
    async def test_slices_include_ancestry_labels(self):
        respx.get("https://example.com/page").mock(
            return_value=httpx.Response(200, text=SEARCHABLE_HTML,
                                        headers={"content-type": "text/html"})
        )
        # Fetch first to populate cache
        await web_fetch_direct("https://example.com/page")
        # Request all slices to check ancestry
        cached = _page_cache.get("https://example.com/page")
        assert cached is not None
        assert cached.slices is not None
        all_indices = list(range(len(cached.slices)))
        result = await web_fetch_direct(
            "https://example.com/page", slices=all_indices
        )
        # At least some slices should have ancestry labels
        assert "(" in result  # parenthetical ancestry
