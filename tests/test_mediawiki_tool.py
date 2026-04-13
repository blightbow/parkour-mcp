"""Tests for the dedicated MediaWiki tool (mediawiki.mediawiki)."""

import httpx
import pytest
import respx

from parkour_mcp.mediawiki import (
    mediawiki,
    _canonicalize_title_for_cache,
    _normalize_citeref_key,
    _resolve_wiki_base,
)
from parkour_mcp._pipeline import _wiki_cache, _page_cache

from .conftest import (
    MEDIAWIKI_QUERY_RESPONSE,
    MEDIAWIKI_PARSE_FULL_RESPONSE,
    MEDIAWIKI_PARSE_WITH_INLINE_CITATIONS,
    MEDIAWIKI_PARSE_WITH_CITATIONS,
    MEDIAWIKI_SEARCH_RESPONSE,
    MEDIAWIKI_SEARCH_EMPTY_RESPONSE,
)
from ._output import (
    assert_fenced,
    split_output,
)


@pytest.fixture(autouse=True)
def clear_caches():
    """Ensure each test starts with empty caches."""
    yield
    _wiki_cache.clear()
    _page_cache.clear()


# --- Pure helpers ---

class TestCanonicalizeTitle:
    """Tests for _canonicalize_title_for_cache."""

    def test_spaces_to_underscores(self):
        assert _canonicalize_title_for_cache("New York City") == "New_York_City"

    def test_lowercase_first_char_is_capitalized(self):
        assert _canonicalize_title_for_cache("new york city") == "New_york_city"

    def test_already_capitalized_preserved(self):
        assert _canonicalize_title_for_cache("IPhone") == "IPhone"

    def test_strips_surrounding_whitespace(self):
        assert _canonicalize_title_for_cache("  Hello World  ") == "Hello_World"

    def test_empty_string(self):
        assert _canonicalize_title_for_cache("") == ""

    def test_non_ascii_first_char(self):
        # Leading non-ASCII letter with uppercase variant
        assert _canonicalize_title_for_cache("gödel's theorem") == "Gödel's_theorem"


class TestNormalizeCiterefKey:
    """Tests for _normalize_citeref_key."""

    def test_hash_fragment_form(self):
        assert _normalize_citeref_key("#CITEREFFoo2005") == "CITEREFFoo2005"

    def test_anchor_id_form(self):
        assert _normalize_citeref_key("CITEREFFoo2005") == "CITEREFFoo2005"

    def test_bare_key(self):
        assert _normalize_citeref_key("Foo2005") == "CITEREFFoo2005"

    def test_non_ascii_key(self):
        assert _normalize_citeref_key("#CITEREFFranzén2005") == "CITEREFFranzén2005"


class TestResolveWikiBase:
    """Tests for _resolve_wiki_base (offline paths only — no probe)."""

    @pytest.mark.asyncio
    async def test_language_code(self):
        host, api_base = await _resolve_wiki_base("en")
        assert host == "en.wikipedia.org"
        assert api_base == "https://en.wikipedia.org/w/api.php"

    @pytest.mark.asyncio
    async def test_multi_part_language_code(self):
        host, api_base = await _resolve_wiki_base("zh-yue")
        assert host == "zh-yue.wikipedia.org"
        assert api_base == "https://zh-yue.wikipedia.org/w/api.php"

    @pytest.mark.asyncio
    async def test_sister_project_alias(self):
        host, api_base = await _resolve_wiki_base("commons")
        assert host == "commons.wikimedia.org"
        assert api_base == "https://commons.wikimedia.org/w/api.php"

    @pytest.mark.asyncio
    async def test_wikimedia_hostname_short_circuit(self):
        """Bare wikimedia hostnames should not trigger a probe."""
        host, api_base = await _resolve_wiki_base("en.wikipedia.org")
        assert host == "en.wikipedia.org"
        assert api_base == "https://en.wikipedia.org/w/api.php"

    @pytest.mark.asyncio
    async def test_full_url_wikimedia_short_circuit(self):
        host, api_base = await _resolve_wiki_base("https://en.wikipedia.org")
        assert host == "en.wikipedia.org"
        assert api_base == "https://en.wikipedia.org/w/api.php"

    @pytest.mark.asyncio
    async def test_empty_raises(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            await _resolve_wiki_base("")

    @pytest.mark.asyncio
    @respx.mock
    async def test_unknown_host_probes_and_fails_gracefully(self):
        """Unknown hosts probe /api.php and /w/api.php; both fail → raise."""
        respx.get("https://example.invalid/api.php").mock(
            return_value=httpx.Response(404)
        )
        respx.get("https://example.invalid/w/api.php").mock(
            return_value=httpx.Response(404)
        )
        with pytest.raises(ValueError, match="no MediaWiki API found"):
            await _resolve_wiki_base("example.invalid")


# --- Dispatcher parameter validation ---

class TestMediaWikiDispatcher:
    """Tests for action routing and title/query parameter validation."""

    @pytest.mark.asyncio
    async def test_unknown_action(self):
        result = await mediawiki(action="bogus", title="Test")
        assert "Error: Unknown action 'bogus'" in result
        assert "page, search, references" in result

    @pytest.mark.asyncio
    async def test_page_without_title(self):
        result = await mediawiki(action="page")
        assert "Error:" in result
        assert "title parameter" in result

    @pytest.mark.asyncio
    async def test_page_with_query_instead_of_title(self):
        result = await mediawiki(action="page", query="Gödel")
        assert "Error:" in result
        assert "title=" in result and "query=" in result

    @pytest.mark.asyncio
    async def test_search_without_query(self):
        result = await mediawiki(action="search")
        assert "Error:" in result
        assert "query parameter" in result

    @pytest.mark.asyncio
    async def test_search_with_title_instead_of_query(self):
        result = await mediawiki(action="search", title="Gödel")
        assert "Error:" in result
        assert "query=" in result and "title=" in result

    @pytest.mark.asyncio
    async def test_references_without_title(self):
        result = await mediawiki(action="references", footnotes=[1])
        assert "Error:" in result
        assert "title parameter" in result

    @pytest.mark.asyncio
    async def test_references_without_footnotes_or_citations(self):
        # Need to mock the fetch since validation happens after URL resolution
        with respx.mock:
            respx.get("https://en.wikipedia.org/api.php").mock(
                side_effect=[
                    httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                    httpx.Response(200, json=MEDIAWIKI_PARSE_FULL_RESPONSE),
                ]
            )
            result = await mediawiki(action="references", title="Test_Page")
        assert "Error:" in result
        assert "footnotes=" in result or "citations=" in result


# --- Page action ---

class TestMediaWikiPage:
    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_by_title(self):
        """Title-based fetch synthesizes a URL and retrieves via the fast path."""
        respx.get("https://en.wikipedia.org/api.php").mock(
            side_effect=[
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                httpx.Response(200, json=MEDIAWIKI_PARSE_FULL_RESPONSE),
            ]
        )
        result = await mediawiki(
            action="page",
            title="Test Page",
            wiki="en",
        )
        fm, fence = split_output(result)
        assert "site: Test Wiki" in fm
        assert "Section One" in fence

    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_by_url_ignores_wiki(self):
        """When title is a URL, the wiki parameter is ignored without error."""
        respx.get("https://custom.example.com/api.php").mock(
            side_effect=[
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                httpx.Response(200, json=MEDIAWIKI_PARSE_FULL_RESPONSE),
            ]
        )
        result = await mediawiki(
            action="page",
            title="https://custom.example.com/wiki/Test_Page",
            wiki="de",  # ignored — URL wins
        )
        _fm, fence = split_output(result)
        assert "Section One" in fence

    @pytest.mark.asyncio
    @respx.mock
    async def test_title_canonicalization_normalizes_spaces(self):
        """'new york city' and 'New_York_City' should hit the same URL."""
        respx.get("https://en.wikipedia.org/api.php").mock(
            side_effect=[
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                httpx.Response(200, json=MEDIAWIKI_PARSE_FULL_RESPONSE),
            ]
        )
        result = await mediawiki(
            action="page",
            title="new york city",
            wiki="en",
        )
        _fm, fence = split_output(result)
        assert "Section One" in fence
        # Cache should now contain the canonical URL form
        cached_urls = list(_wiki_cache._entries.keys())
        assert any("New_york_city" in u for u in cached_urls), cached_urls

    @pytest.mark.asyncio
    @respx.mock
    async def test_section_filter(self):
        """section= parameter narrows the response to matching headings."""
        respx.get("https://en.wikipedia.org/api.php").mock(
            side_effect=[
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                httpx.Response(200, json=MEDIAWIKI_PARSE_FULL_RESPONSE),
            ]
        )
        result = await mediawiki(
            action="page",
            title="Test Page",
            wiki="en",
            section="Section One",
        )
        _fm, fence = split_output(result)
        assert "Section One" in fence
        # Section Two should not appear when we asked for Section One
        assert "Section Two" not in fence

    @pytest.mark.asyncio
    @respx.mock
    async def test_non_wikipedia_instance_probes(self):
        """Unknown host triggers probe path via _detect_mediawiki."""
        # Single endpoint handles both probe and fetch
        respx.get("https://wiki.example.com/api.php").mock(
            side_effect=[
                # Probe from _resolve_wiki_base
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                # Second probe from _cached_mediawiki_fetch (after URL synthesis)
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                # Parse
                httpx.Response(200, json=MEDIAWIKI_PARSE_FULL_RESPONSE),
            ]
        )
        result = await mediawiki(
            action="page",
            title="Test Page",
            wiki="wiki.example.com",
        )
        _fm, fence = split_output(result)
        assert "Section One" in fence


# --- Search action ---

class TestMediaWikiSearch:
    @pytest.mark.asyncio
    @respx.mock
    async def test_successful_search(self):
        respx.get("https://en.wikipedia.org/w/api.php").mock(
            return_value=httpx.Response(200, json=MEDIAWIKI_SEARCH_RESPONSE)
        )
        result = await mediawiki(
            action="search",
            query="Gödel incompleteness",
            wiki="en",
        )
        fm, fence = split_output(result)
        assert_fenced(result)
        assert "action: search" in fm
        assert "total_results: 1337" in fm
        assert "Gödel's incompleteness theorems" in fence
        # searchmatch highlighting preserved as markdown bold
        assert "**Gödel**" in fence
        # hint points at page action for fetching a hit
        assert "hint:" in fm
        assert "page" in fm

    @pytest.mark.asyncio
    @respx.mock
    async def test_empty_results(self):
        respx.get("https://en.wikipedia.org/w/api.php").mock(
            return_value=httpx.Response(200, json=MEDIAWIKI_SEARCH_EMPTY_RESPONSE)
        )
        result = await mediawiki(
            action="search",
            query="xyzzy",
            wiki="en",
        )
        fm, fence = split_output(result)
        assert "total_results: 0" in fm
        assert "No results for" in fence

    @pytest.mark.asyncio
    @respx.mock
    async def test_pagination_via_offset(self):
        """offset= is passed through to the API and reported in the body."""
        route = respx.get("https://en.wikipedia.org/w/api.php").mock(
            return_value=httpx.Response(200, json=MEDIAWIKI_SEARCH_RESPONSE)
        )
        result = await mediawiki(
            action="search",
            query="Gödel",
            wiki="en",
            limit=5,
            offset=10,
        )
        # Verify sroffset and srlimit were sent
        req = route.calls.last.request
        assert "sroffset=10" in str(req.url)
        assert "srlimit=5" in str(req.url)
        # Body reports the offset-aware position
        _fm, fence = split_output(result)
        assert "Showing 11" in fence

    @pytest.mark.asyncio
    @respx.mock
    async def test_limit_clamped_to_50(self):
        """limit > 50 should be clamped to 50 before hitting the API."""
        route = respx.get("https://en.wikipedia.org/w/api.php").mock(
            return_value=httpx.Response(200, json=MEDIAWIKI_SEARCH_RESPONSE)
        )
        await mediawiki(
            action="search",
            query="Gödel",
            wiki="en",
            limit=100,
        )
        req = route.calls.last.request
        assert "srlimit=50" in str(req.url)

    @pytest.mark.asyncio
    @respx.mock
    async def test_namespace_parameter(self):
        """namespace=14 should be forwarded as srnamespace=14."""
        route = respx.get("https://en.wikipedia.org/w/api.php").mock(
            return_value=httpx.Response(200, json=MEDIAWIKI_SEARCH_RESPONSE)
        )
        result = await mediawiki(
            action="search",
            query="animals",
            wiki="en",
            namespace=14,
        )
        req = route.calls.last.request
        assert "srnamespace=14" in str(req.url)
        fm, _fence = split_output(result)
        assert "namespace: 14" in fm

    @pytest.mark.asyncio
    @respx.mock
    async def test_api_error_returns_clean_message(self):
        """An API error should surface as a clean Error: string, not a stack trace."""
        respx.get("https://en.wikipedia.org/w/api.php").mock(
            return_value=httpx.Response(500)
        )
        result = await mediawiki(
            action="search",
            query="Gödel",
            wiki="en",
        )
        assert result.startswith("Error:")
        assert "Search request failed" in result


# --- References action ---

class TestMediaWikiReferences:
    @pytest.mark.asyncio
    @respx.mock
    async def test_footnotes_only(self):
        """footnotes= alone returns numbered footnote entries."""
        respx.get("https://en.wikipedia.org/api.php").mock(
            side_effect=[
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                httpx.Response(200, json=MEDIAWIKI_PARSE_WITH_CITATIONS),
            ]
        )
        result = await mediawiki(
            action="references",
            title="Test Page",
            wiki="en",
            footnotes=[1, 2],
        )
        fm, fence = split_output(result)
        assert "footnotes_only: True" in fm
        assert "references_only" not in fm
        assert "citations_only" not in fm
        assert "## Footnotes" in fence
        assert "First reference source" in fence
        assert "Second reference source" in fence

    @pytest.mark.asyncio
    @respx.mock
    async def test_citations_only(self):
        """citations= alone returns inline CITEREF entries."""
        respx.get("https://en.wikipedia.org/api.php").mock(
            side_effect=[
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                httpx.Response(200, json=MEDIAWIKI_PARSE_WITH_INLINE_CITATIONS),
            ]
        )
        result = await mediawiki(
            action="references",
            title="Test Page",
            wiki="en",
            citations=["#CITEREFFranzén2005"],
        )
        fm, fence = split_output(result)
        assert "citations_only: True" in fm
        assert "references_only" not in fm
        assert "footnotes_only" not in fm
        assert "## Inline citations" in fence
        assert "Franzén, Torkel (2005)" in fence

    @pytest.mark.asyncio
    @respx.mock
    async def test_both_footnotes_and_citations_returns_both(self):
        """Supplying both footnotes and citations returns both blocks in one fence."""
        # Page has both reference types
        combined_html = (
            '<p><a href="#CITEREFFoo2005">Foo (2005)</a> argues.</p>'
            '<cite id="CITEREFFoo2005">Foo, F. (2005). A Book.</cite>'
            '<h2>References</h2>'
            '<ol class="references">'
            '<li><span class="reference-text">First reference.</span></li>'
            '<li><span class="reference-text">Second reference.</span></li>'
            '</ol>'
        )
        respx.get("https://en.wikipedia.org/api.php").mock(
            side_effect=[
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                httpx.Response(200, json={
                    "parse": {
                        "displaytitle": "Combined Page",
                        "text": {"*": combined_html},
                        "sections": [],
                    }
                }),
            ]
        )
        result = await mediawiki(
            action="references",
            title="Combined Page",
            wiki="en",
            footnotes=[1],
            citations=["#CITEREFFoo2005"],
        )
        fm, fence = split_output(result)
        assert "references_only: True" in fm
        # Both single-mode flags still present when both are requested
        assert "footnotes_only: True" in fm
        assert "citations_only: True" in fm
        # Both content blocks present
        assert "## Footnotes" in fence
        assert "## Inline citations" in fence
        # Footnotes block comes before inline citations
        fn_idx = fence.index("## Footnotes")
        ic_idx = fence.index("## Inline citations")
        assert fn_idx < ic_idx
        # Content from both
        assert "First reference" in fence
        assert "Foo, F. (2005)" in fence

    @pytest.mark.asyncio
    @respx.mock
    async def test_footnotes_not_found(self):
        """Requested footnote numbers that don't exist are reported in frontmatter."""
        respx.get("https://en.wikipedia.org/api.php").mock(
            side_effect=[
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                httpx.Response(200, json=MEDIAWIKI_PARSE_WITH_CITATIONS),
            ]
        )
        result = await mediawiki(
            action="references",
            title="Test Page",
            wiki="en",
            footnotes=[1, 99],
        )
        fm, fence = split_output(result)
        assert "footnotes_not_found" in fm
        assert "99" in fm
        # Valid footnote still resolves
        assert "First reference source" in fence

    @pytest.mark.asyncio
    @respx.mock
    async def test_citations_not_found(self):
        """Requested citation keys that don't exist are reported in frontmatter."""
        respx.get("https://en.wikipedia.org/api.php").mock(
            side_effect=[
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                httpx.Response(200, json=MEDIAWIKI_PARSE_WITH_INLINE_CITATIONS),
            ]
        )
        result = await mediawiki(
            action="references",
            title="Test Page",
            wiki="en",
            citations=["#CITEREFFranzén2005", "#CITEREFGhost2099"],
        )
        fm, fence = split_output(result)
        assert "citations_not_found" in fm
        assert "#CITEREFGhost2099" in fm
        assert "Franzén, Torkel (2005)" in fence

    @pytest.mark.asyncio
    @respx.mock
    async def test_citations_accepts_three_key_forms(self):
        """All three key forms (#CITEREF*, CITEREF*, bare) resolve to the same entry."""
        respx.get("https://en.wikipedia.org/api.php").mock(
            side_effect=[
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                httpx.Response(200, json=MEDIAWIKI_PARSE_WITH_INLINE_CITATIONS),
                # Second call uses cache
            ]
        )
        result1 = await mediawiki(
            action="references",
            title="Test Page",
            wiki="en",
            citations=["#CITEREFFranzén2005"],
        )
        _, fence1 = split_output(result1)
        assert "Franzén, Torkel (2005)" in fence1

        # Subsequent calls hit the wiki cache — no new API calls needed.
        result2 = await mediawiki(
            action="references",
            title="Test Page",
            wiki="en",
            citations=["CITEREFFranzén2005"],
        )
        _, fence2 = split_output(result2)
        assert "Franzén, Torkel (2005)" in fence2

        result3 = await mediawiki(
            action="references",
            title="Test Page",
            wiki="en",
            citations=["Franzén2005"],
        )
        _, fence3 = split_output(result3)
        assert "Franzén, Torkel (2005)" in fence3

    @pytest.mark.asyncio
    @respx.mock
    async def test_non_mediawiki_url_error(self):
        """A non-MediaWiki URL should return a specific error."""
        respx.get("https://notawiki.example.com/api.php").mock(
            return_value=httpx.Response(404)
        )
        respx.get("https://notawiki.example.com/w/api.php").mock(
            return_value=httpx.Response(404)
        )
        result = await mediawiki(
            action="references",
            title="https://notawiki.example.com/wiki/Test",
            footnotes=[1],
        )
        assert result.startswith("Error:")
        assert "MediaWiki page" in result
