"""Tests for parkour_mcp.mediawiki module."""

import sys

import httpx
import pytest
import respx

from parkour_mcp.mediawiki import (
    _INLINE_CITEREF_MD_RE,
    _clean_display_title,
    _detect_mediawiki,
    _extract_citations,
    _extract_inline_citations,
    _fetch_mediawiki_page,
    _format_citations,
    _format_inline_citations,
    _format_mediawiki_search,
    _handle_references,
    _handle_search,
    _mediawiki_html_to_markdown,
    _pack_within_budget,
    _probe_api_base,
    _resolve_wiki_base,
)

from .conftest import (
    MEDIAWIKI_PARSE_FULL_RESPONSE,
    MEDIAWIKI_QUERY_MISSING_PAGE,
    MEDIAWIKI_QUERY_RESPONSE,
    MEDIAWIKI_READ_DENIED,
    MEDIAWIKI_SITEINFO_ONLY,
)


@pytest.mark.asyncio
async def test_search_response_declares_trust(monkeypatch):
    """The search action fences result snippets (untrusted wiki content), so
    its frontmatter must declare trust like every other fenced response."""
    async def _fake_base(wiki):
        return ("en.wikipedia.org", "https://en.wikipedia.org/w/api.php")

    async def _fake_search(api_base, query, limit, offset, namespace):
        return ([{"title": "Python", "snippet": "a snippet", "wordcount": 12}], 1)

    # The package namespace rebinds `parkour_mcp.mediawiki` to the tool
    # function, shadowing the submodule, so reach the module via sys.modules
    # (same pattern as test_arxiv / test_semantic_scholar).
    mediawiki_mod = sys.modules["parkour_mcp.mediawiki"]
    monkeypatch.setattr(mediawiki_mod, "_resolve_wiki_base", _fake_base)
    monkeypatch.setattr(mediawiki_mod, "_search_mediawiki", _fake_search)

    result = await _handle_search("python", "en.wikipedia.org", 5, 0, 0, max_tokens=5000)
    assert "trust:" in result
    assert "MediaWiki (en.wikipedia.org)" in result


# --- _detect_mediawiki ---

class TestDetectMediawiki:
    @pytest.mark.asyncio
    async def test_returns_none_for_non_wiki_url(self):
        result = await _detect_mediawiki("https://example.com/page")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_title(self):
        result = await _detect_mediawiki("https://example.com/wiki/")
        assert result is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_detects_valid_mediawiki(self):
        respx.get("https://wiki.example.com/api.php").mock(
            return_value=httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE)
        )

        result = await _detect_mediawiki("https://wiki.example.com/wiki/Test_Page")
        assert result is not None
        assert result["api_base"] == "https://wiki.example.com/api.php"
        assert result["page_title"] == "Test_Page"
        assert result["page_length"] == 5000
        assert result["sitename"] == "Test Wiki"
        assert result["generator"] == "MediaWiki 1.39.7"

    @pytest.mark.asyncio
    @respx.mock
    async def test_falls_back_to_w_api_php(self):
        respx.get("https://wiki.example.com/api.php").mock(
            return_value=httpx.Response(404)
        )
        respx.get("https://wiki.example.com/w/api.php").mock(
            return_value=httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE)
        )

        result = await _detect_mediawiki("https://wiki.example.com/wiki/Test_Page")
        assert result is not None
        assert result["api_base"] == "https://wiki.example.com/w/api.php"

    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_none_for_missing_page(self):
        """A detected wiki with nothing at the requested title still declines,
        because the fast path has no content to render and the generic fetch
        handles the URL.  This is the fast path's contract, not a judgement
        about the host — `_probe_api_base` reports that one found an API."""
        respx.get("https://wiki.example.com/api.php").mock(
            return_value=httpx.Response(200, json=MEDIAWIKI_QUERY_MISSING_PAGE)
        )
        respx.get("https://wiki.example.com/w/api.php").mock(
            return_value=httpx.Response(200, json=MEDIAWIKI_QUERY_MISSING_PAGE)
        )

        result = await _detect_mediawiki("https://wiki.example.com/wiki/Nonexistent_Page")
        assert result is None

        probe = await _probe_api_base("https://wiki.example.com")
        assert probe.state == "found"

    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_none_when_all_probes_fail(self):
        respx.get("https://wiki.example.com/api.php").mock(
            return_value=httpx.Response(500)
        )
        respx.get("https://wiki.example.com/w/api.php").mock(
            return_value=httpx.Response(500)
        )

        result = await _detect_mediawiki("https://wiki.example.com/wiki/Test_Page")
        assert result is None

    @pytest.mark.asyncio
    async def test_url_decodes_page_title(self):
        """Page titles with URL encoding should be decoded."""
        # This will fail the HTTP probe (no mock), but we can check the gate logic
        # by verifying it doesn't return None for a URL with /wiki/ and encoded title
        # We need to mock for a full test
        result = await _detect_mediawiki("https://example.com/not-a-wiki/page")
        assert result is None  # no /wiki/ in path

    @pytest.mark.asyncio
    @respx.mock
    async def test_url_encoded_title(self):
        respx.get("https://wiki.example.com/api.php").mock(
            return_value=httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE)
        )

        result = await _detect_mediawiki("https://wiki.example.com/wiki/Ultima_VIII%20books")
        assert result is not None
        assert result["page_title"] == "Ultima_VIII books"

    @pytest.mark.asyncio
    @respx.mock
    async def test_network_timeout_returns_none(self):
        respx.get("https://wiki.example.com/api.php").mock(
            side_effect=httpx.ConnectTimeout("timeout")
        )
        respx.get("https://wiki.example.com/w/api.php").mock(
            side_effect=httpx.ConnectTimeout("timeout")
        )

        result = await _detect_mediawiki("https://wiki.example.com/wiki/Test_Page")
        assert result is None


# --- _probe_api_base / _resolve_wiki_base states ---

class TestApiProbeStates:
    """Resolving a host asks whether a MediaWiki API answers there.  Every
    other fact — whether some page exists, whether we may read it, whether
    the host likes us — is a separate answer and gets its own state, because
    a caller told "no API found" cannot tell which one it hit."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_generator_alone_identifies_a_wiki(self):
        """The regression: Fandom titles its main page after the wiki, so
        `Main_Page` is missing on langrisser.fandom.com and the old gate read
        that as "no MediaWiki API" — from the same response that carried
        `generator: MediaWiki 1.43.9`.  Nothing about a page may gate this."""
        respx.get("https://wiki.example.com/api.php").mock(
            return_value=httpx.Response(200, json=MEDIAWIKI_SITEINFO_ONLY)
        )

        host, api_base = await _resolve_wiki_base("wiki.example.com")
        assert host == "wiki.example.com"
        assert api_base == "https://wiki.example.com/api.php"

    @pytest.mark.asyncio
    @respx.mock
    async def test_host_probe_names_no_page(self):
        """The synthesized `/wiki/Main_Page` probe is gone.  A request that
        never asks about a page cannot be refused over one."""
        route = respx.get("https://wiki.example.com/api.php").mock(
            return_value=httpx.Response(200, json=MEDIAWIKI_SITEINFO_ONLY)
        )

        await _resolve_wiki_base("wiki.example.com")
        params = route.calls.last.request.url.params
        assert "titles" not in params
        assert params["meta"] == "siteinfo"

    @pytest.mark.asyncio
    @respx.mock
    async def test_denied_read_is_not_a_missing_api(self):
        """A private wiki returns a MediaWiki envelope, so the software is
        positively identified even though the answer is withheld."""
        respx.get("https://wiki.example.com/api.php").mock(
            return_value=httpx.Response(200, json=MEDIAWIKI_READ_DENIED)
        )
        respx.get("https://wiki.example.com/w/api.php").mock(
            return_value=httpx.Response(404)
        )

        probe = await _probe_api_base("https://wiki.example.com")
        assert probe.state == "auth_required"

        with pytest.raises(ValueError, match="not usable anonymously") as exc:
            await _resolve_wiki_base("wiki.example.com")
        assert "readapidenied" in str(exc.value)

    @pytest.mark.asyncio
    @respx.mock
    async def test_other_api_error_reported_as_such(self):
        """Not every MediaWiki error is an auth error.  A wiki shedding load
        under maxlag is still a wiki, and saying "no API found" would be a
        claim about the host the response does not support."""
        respx.get("https://wiki.example.com/api.php").mock(
            return_value=httpx.Response(200, json={
                "error": {"code": "maxlag", "info": "Waiting for a database"},
            })
        )
        respx.get("https://wiki.example.com/w/api.php").mock(
            return_value=httpx.Response(404)
        )

        probe = await _probe_api_base("https://wiki.example.com")
        assert probe.state == "api_error"
        assert "maxlag" in probe.detail

    @pytest.mark.asyncio
    @respx.mock
    async def test_refusal_distinguished_from_absence(self):
        """Fandom 403s every HTML page fetch while leaving api.php open, so a
        403 is a live host rejecting us, not an endpoint that is not there."""
        respx.get("https://wiki.example.com/api.php").mock(
            return_value=httpx.Response(403, text="<html>Forbidden</html>")
        )
        respx.get("https://wiki.example.com/w/api.php").mock(
            return_value=httpx.Response(404)
        )

        with pytest.raises(ValueError, match="refused the probe") as exc:
            await _resolve_wiki_base("wiki.example.com")
        assert "403" in str(exc.value)

    @pytest.mark.asyncio
    @respx.mock
    async def test_most_informative_path_wins(self):
        """Path order must not decide the message: a routine 404 on the path
        we happen to try last cannot bury a 403 on the one before it."""
        respx.get("https://wiki.example.com/api.php").mock(
            return_value=httpx.Response(429)
        )
        respx.get("https://wiki.example.com/w/api.php").mock(
            return_value=httpx.Response(404)
        )

        probe = await _probe_api_base("https://wiki.example.com")
        assert probe.state == "refused"
        assert probe.api_base == "https://wiki.example.com/api.php"

    @pytest.mark.asyncio
    @respx.mock
    async def test_non_json_body_is_not_mediawiki(self):
        respx.get("https://wiki.example.com/api.php").mock(
            return_value=httpx.Response(
                200, text="<html>hello</html>",
                headers={"content-type": "text/html"},
            )
        )
        respx.get("https://wiki.example.com/w/api.php").mock(
            return_value=httpx.Response(404)
        )

        with pytest.raises(ValueError, match="rather than JSON") as exc:
            await _resolve_wiki_base("wiki.example.com")
        assert "text/html" in str(exc.value)

    @pytest.mark.asyncio
    @respx.mock
    async def test_json_without_generator_is_not_mediawiki(self):
        """Some other JSON API answering on /api.php is not a wiki.  The
        generator gate has to reject it, or the tool would go on to issue
        `action=parse` against a stranger."""
        respx.get("https://wiki.example.com/api.php").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        respx.get("https://wiki.example.com/w/api.php").mock(
            return_value=httpx.Response(404)
        )

        with pytest.raises(ValueError, match="named no MediaWiki generator"):
            await _resolve_wiki_base("wiki.example.com")

    @pytest.mark.asyncio
    @respx.mock
    async def test_unreachable_host_says_so(self):
        respx.get("https://wiki.example.com/api.php").mock(
            side_effect=httpx.ConnectError("nope")
        )
        respx.get("https://wiki.example.com/w/api.php").mock(
            side_effect=httpx.ConnectError("nope")
        )

        with pytest.raises(ValueError, match="could not reach") as exc:
            await _resolve_wiki_base("wiki.example.com")
        assert "ConnectError" in str(exc.value)

    @pytest.mark.asyncio
    @respx.mock
    async def test_search_reaches_a_wiki_with_no_main_page(self):
        """The reported failure was `action=search`, which has no URL escape
        hatch: it resolves through the host probe or not at all."""
        respx.get(
            "https://wiki.example.com/api.php", params={"meta": "siteinfo"},
        ).mock(return_value=httpx.Response(200, json=MEDIAWIKI_SITEINFO_ONLY))
        respx.get(
            "https://wiki.example.com/api.php", params={"list": "search"},
        ).mock(return_value=httpx.Response(200, json={
            "query": {
                "search": [
                    {"title": "Safreen", "snippet": "a fairy", "wordcount": 9},
                ],
                "searchinfo": {"totalhits": 1},
            }
        }))

        result = await _handle_search("Safreen", "wiki.example.com", 20, 0, 0, max_tokens=5000)
        assert not result.startswith("Error:")
        # The snippet, not the title: "Safreen" is the query and would echo
        # in frontmatter even if the search route were never reached.
        assert "a fairy" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_absent_hit_count_is_not_reported_as_zero(self):
        """Fandom returns hits with no `searchinfo` block.  Reading that as a
        total of 0 rendered "Showing 1-1 of 0" and, because the follow-up
        hint was keyed off the count, withheld the hint on every result."""
        respx.get(
            "https://wiki.example.com/api.php", params={"meta": "siteinfo"},
        ).mock(return_value=httpx.Response(200, json=MEDIAWIKI_SITEINFO_ONLY))
        respx.get(
            "https://wiki.example.com/api.php", params={"list": "search"},
        ).mock(return_value=httpx.Response(200, json={
            "query": {"search": [{"title": "Safreen", "wordcount": 9}]},
        }))

        result = await _handle_search("Safreen", "wiki.example.com", 20, 0, 0, max_tokens=5000)
        assert "Showing 1–1 on wiki.example.com." in result
        assert "of 0" not in result
        assert "total_results:" not in result
        assert "hint:" in result


# --- max_tokens budgets ---

class TestSearchBudget:
    """The search action never received max_tokens at all, so its output was
    bounded only by limit=.  Results are now packed whole against the budget
    and anything the budget displaced is counted, not quietly missing."""

    def _results(self, n):
        return [
            {"title": f"Page {i}", "pageid": i, "size": 100,
             "wordcount": 500, "timestamp": "", "snippet": "x" * 200}
            for i in range(n)
        ]

    def test_budget_trims_and_counts(self):
        body, omitted = _format_mediawiki_search(
            self._results(20), 20, 0, "q", "wiki.example.com", max_tokens=200,
        )
        assert omitted > 0
        assert len(body) <= 200 * 4
        # The rendered range describes what survived, not what was fetched.
        assert f"Showing 1–{20 - omitted} " in body

    def test_generous_budget_keeps_everything(self):
        body, omitted = _format_mediawiki_search(
            self._results(20), 20, 0, "q", "wiki.example.com", max_tokens=50_000,
        )
        assert omitted == 0
        assert "Showing 1–20 " in body

    def test_first_result_always_survives(self):
        """An empty list is not a smaller answer to a search, it is a
        different one, so the first hit is kept even under an absurd cap."""
        body, omitted = _format_mediawiki_search(
            self._results(20), 20, 0, "q", "wiki.example.com", max_tokens=1,
        )
        assert omitted == 19
        assert "Page 0" in body

    def test_results_are_kept_whole(self):
        """A hit stripped of the snippet that justifies it is worse than a
        hit withheld, so entries are packed whole."""
        body, _ = _format_mediawiki_search(
            self._results(20), 20, 0, "q", "wiki.example.com", max_tokens=200,
        )
        # Every rendered title line is followed by its snippet line.
        titles = body.count("**[Page ")
        snippets = body.count("   " + "x" * 200)
        assert titles == snippets

    @pytest.mark.asyncio
    @respx.mock
    async def test_frontmatter_reports_the_shortfall(self):
        respx.get(
            "https://wiki.example.com/api.php", params={"meta": "siteinfo"},
        ).mock(return_value=httpx.Response(200, json=MEDIAWIKI_SITEINFO_ONLY))
        respx.get(
            "https://wiki.example.com/api.php", params={"list": "search"},
        ).mock(return_value=httpx.Response(200, json={
            "query": {
                "search": [
                    {"title": f"Page {i}", "snippet": "y" * 300, "wordcount": 5}
                    for i in range(10)
                ],
                "searchinfo": {"totalhits": 10},
            }
        }))

        result = await _handle_search(
            "q", "wiki.example.com", 10, 0, 0, max_tokens=200,
        )
        assert "results_omitted:" in result
        assert "raise max_tokens or lower limit=" in result


class TestPackWithinBudget:
    def _entries(self, n):
        return [{"n": i, "text": "z" * 50} for i in range(n)]

    @staticmethod
    def _render(entries):
        return "\n".join(f"[^{e['n']}]: {e['text']}" for e in entries)

    def test_packs_whole_entries(self):
        kept, dropped = _pack_within_budget(self._entries(10), self._render, 200)
        assert kept and dropped
        assert len(kept) + len(dropped) == 10
        assert kept + dropped == self._entries(10)

    def test_generous_budget_drops_nothing(self):
        kept, dropped = _pack_within_budget(self._entries(10), self._render, 10_000)
        assert dropped == []
        assert len(kept) == 10

    def test_oversized_first_entry_still_returned(self):
        """Better an over-budget answer than an empty one, and the caller is
        told what it did not get either way."""
        kept, dropped = _pack_within_budget(self._entries(3), self._render, 1)
        assert len(kept) == 1
        assert len(dropped) == 2


# --- _clean_display_title ---

class TestMediawikiAddressGuard:
    """`wiki` and a URL-shaped `title` both let the caller choose the host,
    so every path that probes one gets the address check.  No respx mock: a
    refused destination must never reach a transport."""

    @pytest.mark.asyncio
    async def test_detect_refuses_loopback(self):
        """The refusal must not be swallowed as 'not a MediaWiki site'.
        Those are different facts, and conflating them both misreports the
        reason and tries the next API path against a refused host."""
        from parkour_mcp.common import BlockedAddress
        with pytest.raises(BlockedAddress, match="127.0.0.1"):
            await _detect_mediawiki("http://127.0.0.1/wiki/Main_Page")

    @pytest.mark.parametrize("wiki", [
        "evil.com#.wikipedia.org",
        "evil.com?.wikimedia.org",
        "evil.com/.wikipedia.org",
        "user@evil.com",
    ])
    @pytest.mark.asyncio
    async def test_rejects_hosts_that_terminate_the_authority(self, wiki):
        """The host is concatenated into API URLs.  'evil.com#.wikipedia.org'
        passed the Wikimedia suffix test, skipped the probe, and built
        'https://evil.com#.wikipedia.org/w/api.php' — which goes out as a
        bare request to evil.com/ with the API path lost in the fragment,
        rendered under an api: label that reads as Wikimedia."""
        with pytest.raises(ValueError, match="hostname or a full URL"):
            await _resolve_wiki_base(wiki)

    @pytest.mark.asyncio
    async def test_accepts_ordinary_hosts(self):
        host, api_base = await _resolve_wiki_base("en.wikipedia.org")
        assert host == "en.wikipedia.org"
        assert api_base == "https://en.wikipedia.org/w/api.php"

    @pytest.mark.asyncio
    async def test_resolve_wiki_base_surfaces_refusal(self):
        """_resolve_wiki_base bridges to ValueError so its callers render an
        error string instead of crashing on a transport exception."""
        with pytest.raises(ValueError, match="private/reserved"):
            await _resolve_wiki_base("127.0.0.1")

    @pytest.mark.asyncio
    async def test_references_reports_the_refusal(self):
        """The references action takes a URL-shaped title verbatim, so it
        reaches a caller-chosen host without going through
        _resolve_wiki_base's https-only construction."""
        result = await _handle_references(
            title="http://127.0.0.1/wiki/X", wiki="en",
            footnotes=[1], citations=None, max_tokens=1000,
        )
        assert result.startswith("Error:")
        assert "private/reserved" in result
        assert "untrusted content" not in result


class TestCleanDisplayTitle:
    def test_strips_html_tags(self):
        assert _clean_display_title("<i>Ultima VIII</i> books") == "Ultima VIII books"

    def test_decodes_html_entities(self):
        assert _clean_display_title("Vol.&#160;II") == "Vol. II"

    def test_normalizes_nbsp(self):
        assert _clean_display_title("Vol.\u00a0II") == "Vol. II"

    def test_combined_tags_and_entities(self):
        assert _clean_display_title("<i>Ultima&#160;VIII</i> books") == "Ultima VIII books"

    def test_plain_title_unchanged(self):
        assert _clean_display_title("Test Page") == "Test Page"


# --- _fetch_mediawiki_page ---

class TestFetchMediawikiPage:
    @pytest.mark.asyncio
    @respx.mock
    async def test_full_page_fetch(self):
        respx.get("https://wiki.example.com/api.php").mock(
            return_value=httpx.Response(200, json=MEDIAWIKI_PARSE_FULL_RESPONSE)
        )

        result = await _fetch_mediawiki_page(
            "https://wiki.example.com/api.php", "Test_Page"
        )
        assert result is not None
        assert result["title"] == "Test Page"
        assert "Section One" in result["html"]
        assert "Section Two" in result["html"]
        assert len(result["sections_meta"]) == 2


# --- _mediawiki_html_to_markdown ---

class TestMediawikiHtmlToMarkdown:
    def test_basic_conversion(self):
        html = "<h2>Title</h2><p>Some content here.</p>"
        result = _mediawiki_html_to_markdown(html)
        assert "Title" in result
        assert "Some content here." in result

    def test_removes_edit_sections(self):
        html = '<h2>Title <span class="mw-editsection">[edit]</span></h2><p>Content.</p>'
        result = _mediawiki_html_to_markdown(html)
        assert "[edit]" not in result
        assert "mw-editsection" not in result

    def test_removes_toc(self):
        html = '<div id="toc"><h2>Contents</h2></div><h2>Real</h2><p>Content.</p>'
        result = _mediawiki_html_to_markdown(html)
        assert "Contents" not in result
        assert "Real" in result

    def test_removes_toc_class(self):
        html = '<div class="toc"><h2>Contents</h2></div><p>Content.</p>'
        result = _mediawiki_html_to_markdown(html)
        assert "Contents" not in result

    def test_removes_scripts_and_styles(self):
        html = '<script>alert("x")</script><style>.x{}</style><p>Content.</p>'
        result = _mediawiki_html_to_markdown(html)
        assert "alert" not in result
        assert ".x{}" not in result
        assert "Content." in result

    def test_collapses_extra_newlines(self):
        html = "<p>A</p><br><br><br><br><p>B</p>"
        result = _mediawiki_html_to_markdown(html)
        assert "\n\n\n" not in result

    def test_converts_inline_citations_to_footnote_markers(self):
        """sup.reference with numeric text becomes [^N] markdown footnote."""
        html = (
            '<p>Some claim.'
            '<sup class="reference"><a href="#cite_note-1">[1]</a></sup>'
            ' Another claim.'
            '<sup class="reference"><a href="#cite_note-2">[2]</a></sup>'
            '</p>'
        )
        result = _mediawiki_html_to_markdown(html)
        assert "[^1]" in result
        assert "[^2]" in result
        assert "[1]" not in result

    def test_strips_non_numeric_ref_markers(self):
        """Non-numeric refs like [nb 1] should be removed, not converted."""
        html = (
            '<p>Text.'
            '<sup class="reference"><a href="#cite_note-nb-1">[nb 1]</a></sup>'
            '</p>'
        )
        result = _mediawiki_html_to_markdown(html)
        assert "[nb 1]" not in result
        assert "Text." in result

    def test_strips_reference_block(self):
        """The .mw-references-wrap footnote block should be removed."""
        html = (
            '<p>Content.</p>'
            '<div class="mw-references-wrap">'
            '<ol class="references"><li>Ref 1</li></ol>'
            '</div>'
        )
        result = _mediawiki_html_to_markdown(html)
        assert "Ref 1" not in result
        assert "Content." in result

    def test_strips_cite_error_paragraphs(self):
        """Cite error paragraphs from incomplete reflist templates are removed."""
        html = (
            '<p>Content.</p>'
            '<p>Cite error: There are ref tags but no reflist.</p>'
        )
        result = _mediawiki_html_to_markdown(html)
        assert "Cite error" not in result
        assert "Content." in result

    def test_strips_editsection_as_heading_sibling(self):
        """Modern MediaWiki wraps [edit] as sibling of heading, not child."""
        html = (
            '<div class="mw-heading mw-heading2">'
            '<h2>Education</h2>'
            '<span class="mw-editsection">'
            '<span class="mw-editsection-bracket">[</span>'
            '<a href="/edit">edit</a>'
            '<span class="mw-editsection-bracket">]</span>'
            '</span>'
            '</div>'
            '<p>Section content.</p>'
        )
        result = _mediawiki_html_to_markdown(html)
        assert "Education" in result
        assert "[edit]" not in result
        assert "edit" not in result or "Education" in result


# --- _extract_citations ---

class TestExtractCitations:
    def test_extracts_numbered_citations(self):
        html = (
            '<ol class="references">'
            '<li><span class="reference-text">First ref.</span></li>'
            '<li><span class="reference-text">Second ref.</span></li>'
            '</ol>'
        )
        citations = _extract_citations(html)
        assert len(citations) == 2
        assert citations[0]["n"] == 1
        assert citations[0]["text"] == "First ref."
        assert citations[1]["n"] == 2

    def test_extracts_external_link(self):
        html = (
            '<ol class="references">'
            '<li><span class="reference-text">'
            '<a class="external" href="https://example.com">Example Title</a>'
            '</span></li>'
            '</ol>'
        )
        citations = _extract_citations(html)
        assert citations[0]["url"] == "https://example.com"
        assert citations[0]["title"] == "Example Title"

    def test_resolves_citeref_bibliography(self):
        """Author-date shorthand should resolve via #CITEREF link."""
        html = (
            '<ol class="references">'
            '<li><span class="reference-text">'
            '<a href="#CITEREFSmith2020">Smith 2020</a>, p. 42.'
            '</span></li>'
            '</ol>'
            '<cite id="CITEREFSmith2020">'
            'Smith, J. (2020). '
            '<a class="external" href="https://example.com/book">The Book</a>.'
            '</cite>'
        )
        citations = _extract_citations(html)
        assert len(citations) == 1
        assert citations[0]["text"] == "Smith 2020 , p. 42."
        assert "sources" in citations[0]
        assert citations[0]["sources"][0]["url"] == "https://example.com/book"
        assert citations[0]["sources"][0]["title"] == "The Book"

    def test_resolves_multiple_citerefs(self):
        """Footnote referencing multiple works resolves all of them."""
        html = (
            '<ol class="references">'
            '<li><span class="reference-text">'
            '<a href="#CITEREFAlpha2020">Alpha 2020</a>, p. 1; '
            '<a href="#CITEREFBeta2021">Beta 2021</a>, p. 2.'
            '</span></li>'
            '</ol>'
            '<cite id="CITEREFAlpha2020">Alpha, A. (2020). Work One.</cite>'
            '<cite id="CITEREFBeta2021">Beta, B. (2021). Work Two.</cite>'
        )
        citations = _extract_citations(html)
        assert len(citations[0]["sources"]) == 2

    def test_no_references_returns_empty(self):
        html = "<p>No references here.</p>"
        assert _extract_citations(html) == []

    def test_picks_largest_reference_list(self):
        """Should use the largest ol.references, skipping small note groups."""
        html = (
            '<ol class="references"><li><span class="reference-text">Note.</span></li></ol>'
            '<ol class="references">'
            '<li><span class="reference-text">Ref 1.</span></li>'
            '<li><span class="reference-text">Ref 2.</span></li>'
            '<li><span class="reference-text">Ref 3.</span></li>'
            '</ol>'
        )
        citations = _extract_citations(html)
        assert len(citations) == 3
        assert citations[0]["text"] == "Ref 1."


# --- _format_citations ---

class TestFormatCitations:
    def test_formats_url_citation(self):
        citations = [{"n": 1, "text": "Title", "url": "https://x.com", "title": "Title"}]
        result = _format_citations(citations)
        assert result == "[^1]: [Title](https://x.com)"

    def test_formats_plain_text_citation(self):
        citations = [{"n": 3, "text": "Smith 2020, p. 42."}]
        result = _format_citations(citations)
        assert result == "[^3]: Smith 2020, p. 42."

    def test_formats_with_resolved_source_url(self):
        citations = [{
            "n": 5, "text": "Smith 2020, p. 42.",
            "sources": [{"text": "Full entry", "url": "https://x.com/book", "title": "The Book"}],
        }]
        result = _format_citations(citations)
        assert "**[The Book](https://x.com/book)**" in result

    def test_formats_with_resolved_source_no_url(self):
        citations = [{
            "n": 7, "text": "Jones 2019, p. 10.",
            "sources": [{"text": "Jones, A. (2019). Some Work. Publisher."}],
        }]
        result = _format_citations(citations)
        assert "*Jones, A. (2019). Some Work. Publisher.*" in result

    def test_formats_multiple_sources(self):
        citations = [{
            "n": 2, "text": "A 2020; B 2021.",
            "sources": [
                {"text": "Alpha.", "url": "https://a.com", "title": "A"},
                {"text": "Beta.", "url": "https://b.com", "title": "B"},
            ],
        }]
        result = _format_citations(citations)
        assert "**[A](https://a.com)**" in result
        assert "**[B](https://b.com)**" in result


# --- _extract_inline_citations ---

class TestExtractInlineCitations:
    def test_extracts_single_inline_citeref(self):
        """Inline author-date anchor resolves to its bibliography entry."""
        html = (
            '<p>Several authors, including '
            '<a href="#CITEREFFranzén2005">Franzén (2005)</a>, '
            'have commented.</p>'
            '<cite id="CITEREFFranzén2005">'
            'Franzén, Torkel (2005). '
            '<a class="external" href="https://example.com/book">Gödel\'s Theorem</a>.'
            '</cite>'
        )
        entries = _extract_inline_citations(html)
        assert len(entries) == 1
        e = entries[0]
        assert e["key"] == "CITEREFFranzén2005"
        assert e["href"] == "#CITEREFFranzén2005"
        assert e["shorthand"] == "Franzén (2005)"
        assert "Franzén, Torkel (2005)" in e["text"]
        assert e["url"] == "https://example.com/book"
        assert e["title"] == "Gödel's Theorem"

    def test_dedupes_repeated_inline_reference(self):
        """Multiple inline uses of the same CITEREF collapse to one entry."""
        html = (
            '<p><a href="#CITEREFSokalBricmont1999">Sokal & Bricmont (1999)</a> '
            'argue. Later, '
            '<a href="#CITEREFSokalBricmont1999">Sokal & Bricmont (1999)</a> '
            'also note.</p>'
            '<cite id="CITEREFSokalBricmont1999">'
            'Sokal, A.; Bricmont, J. (1999). Fashionable Nonsense.'
            '</cite>'
        )
        entries = _extract_inline_citations(html)
        assert len(entries) == 1
        assert entries[0]["key"] == "CITEREFSokalBricmont1999"

    def test_extracts_multiple_distinct_inline_citerefs(self):
        """Two distinct inline CITEREFs produce two entries in document order."""
        html = (
            '<p>See '
            '<a href="#CITEREFAlpha2020">Alpha (2020)</a> and '
            '<a href="#CITEREFBeta2021">Beta (2021)</a>.</p>'
            '<cite id="CITEREFAlpha2020">Alpha, A. (2020). Work One.</cite>'
            '<cite id="CITEREFBeta2021">Beta, B. (2021). Work Two.</cite>'
        )
        entries = _extract_inline_citations(html)
        assert len(entries) == 2
        assert entries[0]["key"] == "CITEREFAlpha2020"
        assert entries[1]["key"] == "CITEREFBeta2021"

    def test_skips_anchors_inside_references_block(self):
        """CITEREFs inside .mw-references-wrap are handled by footnote path,
        not the inline path, to avoid double-counting."""
        html = (
            '<div class="mw-references-wrap">'
            '<ol class="references">'
            '<li><span class="reference-text">'
            '<a href="#CITEREFInsideRef2020">Inside (2020)</a>, p. 1.'
            '</span></li>'
            '</ol>'
            '</div>'
            '<p>Prose with <a href="#CITEREFOutside2021">Outside (2021)</a>.</p>'
            '<cite id="CITEREFOutside2021">Outside, O. (2021). Prose Work.</cite>'
            '<cite id="CITEREFInsideRef2020">Inside, I. (2020). Footnote Work.</cite>'
        )
        entries = _extract_inline_citations(html)
        assert len(entries) == 1
        assert entries[0]["key"] == "CITEREFOutside2021"

    def test_skips_unresolvable_citeref(self):
        """Anchor pointing at a missing bibliography target is dropped."""
        html = (
            '<p><a href="#CITEREFGhost1999">Ghost (1999)</a> said so.</p>'
        )
        entries = _extract_inline_citations(html)
        assert entries == []

    def test_mediawiki_markdown_preserves_native_citeref_links(self):
        """The HTML→markdown pass leaves inline CITEREFs as native markdown
        links — provenance is carried by the link itself, not an invented
        marker."""
        html = (
            '<p>See <a href="#CITEREFFoo2005">Foo (2005)</a> for details.</p>'
        )
        md_out = _mediawiki_html_to_markdown(html)
        assert "[Foo (2005)](#CITEREFFoo2005)" in md_out

    def test_inline_citeref_md_regex_counts_matches(self):
        """The regex used for the JIT advisory matches the markdown form."""
        md_text = (
            "Several authors — [Franzén (2005)](#CITEREFFranzén2005), "
            "[Sokal & Bricmont (1999)](#CITEREFSokalBricmont1999) — "
            "commented."
        )
        matches = _INLINE_CITEREF_MD_RE.findall(md_text)
        assert len(matches) == 2
        assert matches[0][1] == "#CITEREFFranzén2005"
        assert matches[1][1] == "#CITEREFSokalBricmont1999"


# --- _format_inline_citations ---

class TestFormatInlineCitations:
    def test_formats_single_entry_with_external_link(self):
        entries = [{
            "key": "CITEREFFoo2005",
            "href": "#CITEREFFoo2005",
            "shorthand": "Foo (2005)",
            "text": "Foo, F. (2005). A Book.",
            "url": "https://example.com/book",
            "title": "A Book",
        }]
        result = _format_inline_citations(entries)
        # First line reproduces the anchor shape so the caller can map it
        # back to the in-prose reference.
        assert "[Foo (2005)](#CITEREFFoo2005)" in result
        assert "Foo, F. (2005). A Book." in result
        assert "**[A Book](https://example.com/book)**" in result

    def test_formats_entry_without_external_link(self):
        entries = [{
            "key": "CITEREFPlainAuthor2020",
            "href": "#CITEREFPlainAuthor2020",
            "shorthand": "Author (2020)",
            "text": "Author, A. (2020). No link attached.",
        }]
        result = _format_inline_citations(entries)
        assert "[Author (2020)](#CITEREFPlainAuthor2020)" in result
        assert "Author, A. (2020). No link attached." in result
        # No external link line.
        assert "**" not in result
