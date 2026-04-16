"""Tests for content slicing, keyword search, and slice retrieval."""

import httpx
import pytest
import respx

from parkour_mcp.fetch_direct import web_fetch_direct
from parkour_mcp.markdown import (
    _compute_slice_ancestry,
)
from parkour_mcp._pipeline import _page_cache, _wiki_cache


@pytest.fixture(autouse=True)
def clear_caches():
    """Ensure each test starts with empty caches."""
    yield
    _wiki_cache.clear()
    _page_cache.clear()


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
# _WikiCache unit tests
# ---------------------------------------------------------------------------

class TestWikiCache:
    def test_store_and_get(self):
        _wiki_cache.store("https://en.wikipedia.org/wiki/Test", {"api_base": "x"}, {"html": "<p>hi</p>"})
        info, page = _wiki_cache.get("https://en.wikipedia.org/wiki/Test")
        assert info == {"api_base": "x"}
        assert page == {"html": "<p>hi</p>"}

    def test_miss(self):
        _wiki_cache.store("https://en.wikipedia.org/wiki/A", {}, {})
        info, page = _wiki_cache.get("https://en.wikipedia.org/wiki/B")
        assert info is None
        assert page is None

    def test_multi_entry(self):
        _wiki_cache.store("https://en.wikipedia.org/wiki/A", {"a": 1}, None)
        _wiki_cache.store("https://en.wikipedia.org/wiki/B", {"b": 2}, None)
        info_a, _ = _wiki_cache.get("https://en.wikipedia.org/wiki/A")
        info_b, _ = _wiki_cache.get("https://en.wikipedia.org/wiki/B")
        assert info_a == {"a": 1}
        assert info_b == {"b": 2}

    def test_clear(self):
        _wiki_cache.store("https://en.wikipedia.org/wiki/A", {}, {})
        _wiki_cache.clear()
        info, page = _wiki_cache.get("https://en.wikipedia.org/wiki/A")
        assert info is None


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

    def test_multi_entry_coexistence(self):
        """Multiple URLs coexist in the cache."""
        _page_cache.store("https://first.com", "First", "# First")
        _page_cache.store("https://second.com", "Second", "# Second")
        first = _page_cache.get("https://first.com")
        second = _page_cache.get("https://second.com")
        assert first is not None
        assert first.title == "First"
        assert second is not None
        assert second.title == "Second"

    def test_new_entries_land_in_probation(self):
        """A freshly stored entry is in probation, not protected."""
        from parkour_mcp._pipeline import _PageCache
        cache = _PageCache(max_entries=3)
        cache.store("https://a.com", "A", "# A")
        assert "https://a.com" in cache._probation
        assert "https://a.com" not in cache._protected

    def test_get_promotes_to_protected(self):
        """Accessing a probation entry promotes it to protected."""
        from parkour_mcp._pipeline import _PageCache
        cache = _PageCache(max_entries=3)
        cache.store("https://a.com", "A", "# A")
        cache.get("https://a.com")
        assert "https://a.com" not in cache._probation
        assert "https://a.com" in cache._protected

    def test_eviction_prefers_probation(self):
        """Probation entries are evicted before protected entries."""
        from parkour_mcp._pipeline import _PageCache
        cache = _PageCache(max_entries=3)
        cache.store("https://a.com", "A", "# A")
        cache.get("https://a.com")  # promote A to protected
        cache.store("https://b.com", "B", "# B")  # probation
        cache.store("https://c.com", "C", "# C")  # probation
        # Full at 3. Storing D should evict B (oldest probation), not A (protected).
        cache.store("https://d.com", "D", "# D")
        assert cache.get("https://a.com") is not None  # protected, safe
        assert cache.get("https://b.com") is None      # evicted from probation
        assert cache.get("https://c.com") is not None
        assert cache.get("https://d.com") is not None

    def test_protected_lru_eviction(self):
        """When probation is empty, the oldest protected entry is evicted."""
        from parkour_mcp._pipeline import _PageCache
        cache = _PageCache(max_entries=3)
        # Fill with all-promoted entries
        cache.store("https://a.com", "A", "# A")
        cache.get("https://a.com")  # promote
        cache.store("https://b.com", "B", "# B")
        cache.get("https://b.com")  # promote
        cache.store("https://c.com", "C", "# C")
        cache.get("https://c.com")  # promote
        # All 3 in protected, probation empty. Storing D evicts oldest protected (A).
        cache.store("https://d.com", "D", "# D")
        assert cache.get("https://a.com") is None
        assert cache.get("https://b.com") is not None
        assert cache.get("https://c.com") is not None
        assert cache.get("https://d.com") is not None

    def test_scan_resistance(self):
        """One-hit pages in probation don't evict drilled-into protected pages."""
        from parkour_mcp._pipeline import _PageCache
        cache = _PageCache(max_entries=4)
        # Two pages the user drills into (promoted to protected)
        cache.store("https://working-a.com", "A", "# A")
        cache.get("https://working-a.com")
        cache.store("https://working-b.com", "B", "# B")
        cache.get("https://working-b.com")
        # Scan: browse through several one-hit pages
        cache.store("https://scan-1.com", "S1", "# S1")
        cache.store("https://scan-2.com", "S2", "# S2")
        # Full at 4. Another scan page should evict scan-1 (probation), not working pages.
        cache.store("https://scan-3.com", "S3", "# S3")
        assert cache.get("https://working-a.com") is not None
        assert cache.get("https://working-b.com") is not None
        assert cache.get("https://scan-1.com") is None  # evicted from probation

    def test_renderer_filter(self):
        """get() with renderer filter only returns matching entries."""
        _page_cache.store("https://example.com", "Title", "# Content", renderer="direct")
        assert _page_cache.get("https://example.com") is not None
        assert _page_cache.get("https://example.com", renderer="direct") is not None
        assert _page_cache.get("https://example.com", renderer="js") is None

    def test_renderer_filter_no_promotion(self):
        """get() with non-matching renderer does not promote the entry."""
        from parkour_mcp._pipeline import _PageCache
        cache = _PageCache(max_entries=5)
        cache.store("https://example.com", "Title", "# Content", renderer="direct")
        # This should return None (renderer mismatch) and NOT promote
        assert cache.get("https://example.com", renderer="js") is None
        assert "https://example.com" in cache._probation
        assert "https://example.com" not in cache._protected

    def test_store_same_url_updates_in_place(self):
        """Storing the same URL replaces the entry without evicting others."""
        from parkour_mcp._pipeline import _PageCache
        cache = _PageCache(max_entries=3)
        cache.store("https://a.com", "A", "# A")
        cache.store("https://b.com", "B", "# B")
        cache.store("https://a.com", "A-updated", "# A updated")
        # Both should exist, A should have the updated title
        assert cache.get("https://b.com") is not None
        a = cache.get("https://a.com")
        assert a is not None
        assert a.title == "A-updated"

    def test_store_updates_protected_in_place(self):
        """Re-storing a URL that was already promoted keeps it in protected."""
        from parkour_mcp._pipeline import _PageCache
        cache = _PageCache(max_entries=3)
        cache.store("https://a.com", "A", "# A")
        cache.get("https://a.com")  # promote to protected
        cache.store("https://a.com", "A-v2", "# A v2")
        assert "https://a.com" in cache._protected
        entry = cache.get("https://a.com")
        assert entry is not None
        assert entry.title == "A-v2"

    def test_group_eviction(self):
        """Evicting one entry evicts all entries in its group."""
        from parkour_mcp._pipeline import _PageCache
        cache = _PageCache(max_entries=4)
        cache.store("https://pr/1/comments", "PR comments", "# Comments", group="pr:1")
        cache.store("https://pr/1/code", "PR code", "# Code", group="pr:1")
        cache.store("https://other.com", "Other", "# Other")
        cache.store("https://another.com", "Another", "# Another")
        # Full at 4. Storing a 5th evicts the oldest probation group —
        # both pr:1 entries — freeing 2 slots.
        cache.store("https://new.com", "New", "# New")
        assert cache.get("https://pr/1/comments") is None
        assert cache.get("https://pr/1/code") is None
        assert cache.get("https://other.com") is not None
        assert cache.get("https://another.com") is not None
        assert cache.get("https://new.com") is not None

    def test_group_eviction_across_queues(self):
        """Group eviction removes members from both probation and protected."""
        from parkour_mcp._pipeline import _PageCache
        cache = _PageCache(max_entries=4)
        cache.store("https://pr/1/a", "A", "# A", group="pr:1")
        cache.get("https://pr/1/a")  # promote to protected
        cache.store("https://pr/1/b", "B", "# B", group="pr:1")  # stays in probation
        cache.store("https://x.com", "X", "# X")
        cache.get("https://x.com")  # promote
        cache.store("https://y.com", "Y", "# Y")
        # Full at 4. pr/1/b is oldest in probation. Evicting it should
        # also evict pr/1/a from protected (same group).
        cache.store("https://z.com", "Z", "# Z")
        assert cache.get("https://pr/1/a") is None
        assert cache.get("https://pr/1/b") is None
        assert cache.get("https://x.com") is not None

    def test_clear(self):
        """clear() empties all entries from both queues."""
        from parkour_mcp._pipeline import _PageCache
        cache = _PageCache(max_entries=5)
        cache.store("https://a.com", "A", "# A")
        cache.get("https://a.com")  # promote to protected
        cache.store("https://b.com", "B", "# B")  # stays in probation
        cache.clear()
        assert cache.get("https://a.com") is None
        assert cache.get("https://b.com") is None
        assert len(cache._probation) == 0
        assert len(cache._protected) == 0

    def test_entry_estimated_bytes(self):
        """_CacheEntry.estimated_bytes returns a positive size estimate."""
        md = "# Title\n\n" + "Some content. " * 100
        _page_cache.store("https://example.com", "Title", md)
        cached = _page_cache.get("https://example.com")
        assert cached is not None
        assert cached.estimated_bytes > len(md)  # slices + ancestry + tantivy estimate

    def test_stats_structure(self):
        """stats property returns queue distribution and per-entry info."""
        from parkour_mcp._pipeline import _PageCache
        cache = _PageCache(max_entries=5)
        cache.store("https://a.com", "A", "# A content here")
        cache.store("https://b.com", "B", "# B content here")
        cache.get("https://a.com")  # promote to protected
        stats = cache.stats
        assert stats["max_entries"] == 5
        assert stats["total_entries"] == 2
        assert stats["probation_entries"] == 1
        assert stats["protected_entries"] == 1
        assert stats["total_estimated_bytes"] > 0
        assert len(stats["entries"]) == 2
        # Check per-entry info
        urls = {e["url"] for e in stats["entries"]}
        assert urls == {"https://a.com", "https://b.com"}
        queues = {e["url"]: e["queue"] for e in stats["entries"]}
        assert queues["https://a.com"] == "protected"
        assert queues["https://b.com"] == "probation"

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
# Lazy build + circuit breaker (issue #6)
# ---------------------------------------------------------------------------

class _CountingSplitter:
    """Spy wrapper for _CacheEntry._SPLITTER.

    MarkdownSplitter is a Rust-backed PyO3 object; its methods are
    read-only so ``monkeypatch.setattr(_SPLITTER, "chunk_indices", …)``
    fails.  Patching the class attribute ``_CacheEntry._SPLITTER`` with
    a Python wrapper lets us count invocations without touching the
    Rust object.
    """

    def __init__(self, real):
        self._real = real
        self.call_count = 0

    def chunk_indices(self, markdown):
        self.call_count += 1
        return self._real.chunk_indices(markdown)


def _install_counting_splitter(monkeypatch):
    from parkour_mcp import _pipeline as pipeline_mod
    spy = _CountingSplitter(pipeline_mod._CacheEntry._SPLITTER)
    monkeypatch.setattr(pipeline_mod._CacheEntry, "_SPLITTER", spy)
    return spy


class TestLazyCacheEntry:
    """``_CacheEntry`` defers MarkdownSplitter + tantivy build until first
    access to slices, slice_ancestry, search(), or build_failed.  A
    pre-scan circuit breaker refuses to run the splitter on single-line
    content over 1 MB (issue #6 — pathological input hangs the event
    loop for multi-second CPU bursts).
    """

    def test_store_does_not_trigger_splitter(self, monkeypatch):
        spy = _install_counting_splitter(monkeypatch)

        _page_cache.store(
            "https://example.com/a", "Title",
            "# T\n\nparagraph one.\n\nparagraph two.\n",
        )
        # Splitter must not fire during store — that's the whole point
        # of Option C.  Consumers of slicing pay the cost on demand.
        assert spy.call_count == 0

    def test_slices_access_triggers_build_exactly_once(self, monkeypatch):
        spy = _install_counting_splitter(monkeypatch)

        _page_cache.store(
            "https://example.com/b", "Title",
            "# T\n\nparagraph one.\n\nparagraph two.\n",
        )
        cached = _page_cache.get("https://example.com/b")
        assert cached is not None
        assert not cached.is_built

        _ = cached.slices
        assert spy.call_count == 1
        assert cached.is_built

        # Subsequent accesses must not rebuild.
        _ = cached.slices
        _ = cached.slice_ancestry
        assert spy.call_count == 1

    def test_search_triggers_build(self):
        _page_cache.store(
            "https://example.com/c", "Doc",
            "# Doc\n\nWe used the frobnicate algorithm extensively.\n",
        )
        cached = _page_cache.get("https://example.com/c")
        assert cached is not None
        assert not cached.is_built

        results = cached.search("frobnicate")
        assert cached.is_built
        assert len(results) >= 1

    def test_pathological_markdown_trips_circuit_breaker(self, monkeypatch):
        """2 MiB single line → ``_safe_markdown_presplit`` returns None →
        entry marked ``_build_failed`` → slices empty, search empty.

        The splitter spy must NOT be invoked — the whole point of the
        circuit breaker is to short-circuit before MarkdownSplitter's
        char-level fallback can hang the loop.
        """
        spy = _install_counting_splitter(monkeypatch)

        pathological = "x" * (2 * 1024 * 1024)
        _page_cache.store("https://example.com/d", "Bad", pathological)
        cached = _page_cache.get("https://example.com/d")
        assert cached is not None

        # Accessing .slices triggers the build attempt
        assert cached.slices == []
        assert cached.is_built is False
        assert cached.build_failed is True
        assert cached.search("x") == []
        # Splitter was never invoked — breaker tripped before the call.
        assert spy.call_count == 0

    def test_estimated_bytes_cheap_when_lazy(self):
        md = "# Title\n\n" + ("Some content. " * 500)
        _page_cache.store("https://example.com/e", "Title", md)
        cached = _page_cache.get("https://example.com/e")
        assert cached is not None
        assert not cached.is_built

        lazy_estimate = cached.estimated_bytes
        assert lazy_estimate > 0
        # Cheap lazy estimate: ~3x markdown size.  Must not force build.
        assert not cached.is_built

        # Force build, re-check: exact accounting should be in the same
        # ballpark as the cheap estimate (0.5x–2x).
        _ = cached.slices
        assert cached.is_built
        built_estimate = cached.estimated_bytes
        assert 0.5 * built_estimate <= lazy_estimate <= 2 * built_estimate


class TestSafeMarkdownPresplit:
    """Circuit-breaker helper for issue #6."""

    def test_returns_chunks_for_normal_markdown(self):
        from parkour_mcp._pipeline import _safe_markdown_presplit

        md = "# Title\n\n" + "A normal paragraph. " * 200 + "\n\n"
        result = _safe_markdown_presplit(md)
        assert result is not None
        assert len(result) >= 1
        # Each entry is (offset, text)
        for offset, text in result:
            assert isinstance(offset, int)
            assert isinstance(text, str)

    def test_trips_on_single_mb_line(self):
        from parkour_mcp._pipeline import _safe_markdown_presplit

        pathological = "x" * (2 * 1024 * 1024)
        assert _safe_markdown_presplit(pathological) is None

    def test_passes_whatwg_fixture(self):
        """Structural real-world content (WHATWG HTML spec) must not
        trip the breaker — legitimate markdown never has 1 MB lines."""
        import gzip
        from pathlib import Path

        fixture = (
            Path(__file__).parent / "fixtures" / "perf" / "whatwg_html.html.gz"
        )
        if not fixture.exists():
            pytest.skip(f"Fixture {fixture} not present")
        with gzip.open(fixture, "rt", encoding="utf-8") as f:
            whatwg_html = f.read()

        from parkour_mcp.markdown import html_to_markdown
        from parkour_mcp._pipeline import _safe_markdown_presplit

        _, markdown = html_to_markdown(whatwg_html)
        result = _safe_markdown_presplit(markdown)
        # Well-formed spec content — breaker stays closed, splitter runs.
        assert result is not None
        assert len(result) > 10  # WHATWG produces thousands of chunks

    def test_boundary(self):
        """Threshold is inclusive: a line exactly at max_line_chars passes;
        one char over fails.  Exposed via the max_line_chars parameter so
        the test doesn't need to allocate a real 1 MB buffer."""
        from parkour_mcp._pipeline import _safe_markdown_presplit

        just_ok = "a" * 99 + "\n"  # line_len = 100 (nl - pos at nl=99 gives 99)
        assert _safe_markdown_presplit(just_ok, max_line_chars=99) is not None

        too_long = "a" * 101 + "\n"  # line 101 chars → trips
        assert _safe_markdown_presplit(too_long, max_line_chars=99) is None


# ---------------------------------------------------------------------------
# Fail-closed frontmatter when the circuit breaker has tripped
# ---------------------------------------------------------------------------

class TestUnbuildablePageResponse:
    """``search=``/``slices=`` against a cached-but-unbuildable page must
    emit a structured frontmatter response instead of a bare empty
    result — principle of least astonishment.  The LLM sees a ``note:``
    explaining *why* slicing is unavailable and a ``hint:`` pointing at
    the next viable action (``section=`` or higher ``max_tokens``).
    """

    @pytest.mark.asyncio
    @respx.mock
    async def test_search_surfaces_fail_closed_frontmatter(self):
        # 2 MiB of ``x`` inside a single <p> produces a single 2 MiB
        # markdown line after html_to_markdown — trips the circuit
        # breaker in _safe_markdown_presplit.
        pathological = "<p>" + ("x" * (2 * 1024 * 1024)) + "</p>"
        html = f"<html><body><h1>Overview</h1>{pathological}</body></html>"
        respx.get("https://example.com/bad-search").mock(
            return_value=httpx.Response(
                200, text=html,
                headers={"content-type": "text/html; charset=utf-8"},
            )
        )

        result = await web_fetch_direct(
            "https://example.com/bad-search", search="anything",
        )
        # Must not be the bare cache-miss error string
        assert "Error: Page cache unavailable." not in result
        # Structured frontmatter signalling
        assert "note:" in result
        assert "BM25 slicing" in result
        assert "hint:" in result
        assert "section=" in result
        assert "max_tokens" in result
        assert "trust:" in result
        # Distinguishes "search ran, nothing found" (which uses "none")
        # from "can't run search at all" (which uses "unavailable").
        assert "matched_slices: unavailable" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_slices_surfaces_fail_closed_frontmatter(self):
        pathological = "<p>" + ("x" * (2 * 1024 * 1024)) + "</p>"
        html = f"<html><body><h1>Overview</h1>{pathological}</body></html>"
        respx.get("https://example.com/bad-slices").mock(
            return_value=httpx.Response(
                200, text=html,
                headers={"content-type": "text/html; charset=utf-8"},
            )
        )

        result = await web_fetch_direct(
            "https://example.com/bad-slices", slices=[0, 1, 2],
        )
        assert "Error: Page cache unavailable." not in result
        assert "note:" in result
        assert "BM25 slicing" in result
        assert "hint:" in result
        assert "section=" in result
        assert "max_tokens" in result
        assert "trust:" in result
        assert "slices_not_found: unavailable" in result


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
