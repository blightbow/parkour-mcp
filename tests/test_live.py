"""Live integration tests that hit real endpoints.

Skipped by default. Run with:
    uv run pytest tests/test_live.py -v
    uv run pytest -m live -v
"""

import pytest

from parkour_mcp.fetch_direct import web_fetch_direct
from parkour_mcp.markdown import _extract_sections_from_markdown
from parkour_mcp.mediawiki import (
    _detect_mediawiki,
    _fetch_mediawiki_page,
    _mediawiki_html_to_markdown,
)

from ._output import (
    assert_fenced,
    fenced_heading,
    fenced_line,
    split_output,
)

pytestmark = pytest.mark.live

WIKI_URL = "https://wiki.ultimacodex.com/wiki/Ultima_VIII_books"


# --- MediaWiki detection ---

class TestLiveMediawikiDetection:
    @pytest.mark.requires_live("ultimacodex")
    @pytest.mark.asyncio
    async def test_detects_ultimacodex_wiki(self):
        result = await _detect_mediawiki(WIKI_URL)
        assert result is not None
        assert result["api_base"] == "https://wiki.ultimacodex.com/api.php"
        assert result["page_title"] == "Ultima_VIII_books"
        assert result["page_length"] > 0
        assert result["sitename"] == "Ultima Codex"
        assert "MediaWiki" in result["generator"]

    @pytest.mark.requires_live("httpbin")
    @pytest.mark.asyncio
    async def test_non_wiki_url_returns_none_fast(self):
        result = await _detect_mediawiki("https://httpbin.org/html")
        assert result is None


# --- MediaWiki page fetch ---

@pytest.mark.requires_live("ultimacodex")
class TestLiveMediawikiPageFetch:
    @pytest.mark.asyncio
    async def test_full_page_fetch(self):
        info = await _detect_mediawiki(WIKI_URL)
        assert info is not None

        page = await _fetch_mediawiki_page(info["api_base"], info["page_title"])
        assert page is not None
        assert page["title"]
        assert len(page["html"]) > 1000
        assert len(page["sections_meta"]) > 10



# --- MediaWiki HTML → markdown ---

@pytest.mark.requires_live("ultimacodex")
class TestLiveMediawikiMarkdown:
    @pytest.mark.asyncio
    async def test_full_page_to_markdown(self):
        info = await _detect_mediawiki(WIKI_URL)
        assert info is not None
        page = await _fetch_mediawiki_page(info["api_base"], info["page_title"])
        assert page is not None
        md = _mediawiki_html_to_markdown(page["html"])

        assert len(md) > 1000
        assert "\n\n\n" not in md  # no triple newlines

        sections = _extract_sections_from_markdown(md)
        names = [s["name"] for s in sections]
        assert "Honor Lost" in names
        assert "The Spell of Divination" in names


# --- web_fetch_direct ---

class TestLiveWebFetchDirect:
    @pytest.mark.requires_live("ultimacodex")
    @pytest.mark.asyncio
    async def test_wiki_full_page_truncated(self):
        result = await web_fetch_direct(WIKI_URL, max_tokens=200)
        fm, fence = split_output(result)

        # Frontmatter: trusted server-generated metadata only.
        assert "site: Ultima Codex" in fm
        assert "generator: MediaWiki" in fm
        assert "truncated:" in fm

        # Security invariant: attacker-controlled page title must not
        # leak into the trusted frontmatter zone.
        assert "Ultima VIII" not in fm
        assert "title:" not in fm

        # Fenced content: title as heading, sections list, body text.
        assert_fenced(result)
        assert fenced_heading(1, "Ultima VIII books") in fence
        assert fenced_line("Sections:") in fence
        assert "Honor Lost" in fence

    @pytest.mark.requires_live("ultimacodex")
    @pytest.mark.asyncio
    async def test_wiki_single_section(self):
        result = await web_fetch_direct(WIKI_URL, section="Honor Lost", max_tokens=500)
        _fm, fence = split_output(result)
        assert_fenced(result)
        # Section rendered as a markdown heading inside the fence.
        assert fenced_heading(4, "Honor Lost") in fence
        assert "Meltzars" in fence

    @pytest.mark.requires_live("ultimacodex")
    @pytest.mark.asyncio
    async def test_wiki_multiple_sections(self):
        result = await web_fetch_direct(
            WIKI_URL,
            section=["Honor Lost", "The Spell of Divination"],
            max_tokens=2000,
        )
        _fm, fence = split_output(result)
        assert_fenced(result)
        assert fenced_heading(4, "Honor Lost") in fence
        assert fenced_heading(5, "The Spell of Divination") in fence

    @pytest.mark.requires_live("httpbin")
    @pytest.mark.asyncio
    async def test_json_endpoint(self):
        result = await web_fetch_direct("https://httpbin.org/json")
        assert "content_type: json" in result
        assert "slideshow" in result

    @pytest.mark.requires_live("httpbin")
    @pytest.mark.asyncio
    async def test_html_endpoint_markdown_default(self):
        result = await web_fetch_direct("https://httpbin.org/html")
        _fm, fence = split_output(result)
        assert_fenced(result)
        # Title rendered as a heading inside the fence.
        assert fenced_heading(1, "Herman Melville - Moby-Dick") in fence
        assert "<document" not in result  # not XML

    @pytest.mark.requires_live("httpbin")
    @pytest.mark.asyncio
    async def test_404_returns_error(self):
        result = await web_fetch_direct("https://httpbin.org/status/404")
        assert "Error:" in result
        assert "404" in result


# --- requires_js (headless-browser render) ---

class TestLiveRequiresJs:
    @pytest.mark.requires_live("ultimacodex")
    @pytest.mark.asyncio
    async def test_wiki_full_page_via_api(self):
        """MediaWiki fast path should return content without launching browser."""
        result = await web_fetch_direct(WIKI_URL, requires_js=True, max_tokens=200)
        fm, fence = split_output(result)

        assert "site: Ultima Codex" in fm
        assert "generator: MediaWiki" in fm
        assert "truncated:" in fm

        assert_fenced(result)
        assert fenced_heading(1, "Ultima VIII books") in fence
        assert fenced_line("Sections:") in fence
        assert "Honor Lost" in fence

    @pytest.mark.requires_live("ultimacodex")
    @pytest.mark.asyncio
    async def test_wiki_section_fetch_via_api(self):
        result = await web_fetch_direct(
            WIKI_URL, section="Honor Lost", requires_js=True, max_tokens=1000
        )
        _fm, fence = split_output(result)
        assert_fenced(result)
        assert fenced_heading(4, "Honor Lost") in fence
        assert "Meltzars" in fence
        # Should NOT contain browser: key (fast path skips browser)
        assert "browser:" not in result

    @pytest.mark.requires_live("ultimacodex")
    @pytest.mark.asyncio
    async def test_wiki_multiple_sections_via_api(self):
        result = await web_fetch_direct(
            WIKI_URL,
            section=["Honor Lost", "The Spell of Divination"],
            requires_js=True,
            max_tokens=2000,
        )
        _fm, fence = split_output(result)
        assert_fenced(result)
        assert fenced_heading(4, "Honor Lost") in fence
        assert fenced_heading(5, "The Spell of Divination") in fence

    @pytest.mark.requires_live("httpbin")
    @pytest.mark.asyncio
    async def test_non_wiki_uses_browser(self):
        """Non-wiki URL should fall through to the browser path."""
        result = await web_fetch_direct(
            "https://httpbin.org/html", requires_js=True, max_tokens=500
        )
        assert "browser:" in result
        assert "generator: MediaWiki" not in result

    @pytest.mark.requires_live("httpbin")
    @pytest.mark.asyncio
    async def test_premature_requires_js_emits_tip(self):
        """Mechanism #3: cold requires_js with no JS-shell evidence fires the tip."""
        result = await web_fetch_direct(
            "https://httpbin.org/html", requires_js=True, max_tokens=500
        )
        assert "tip:" in result
        assert "detects JavaScript-shell pages" in result


class TestLiveAkamaiHttp2:
    """HTTP/2 + browser-coherent headers clear Akamai Bot Manager 403s."""

    # Akamai-fronted PDF on Whirlpool's CDN.  Over HTTP/1.1 with a bare UA it
    # 403s; HTTP/2 plus the Sec-Fetch/Client-Hint headers clear it.  Asserted at
    # the guarded_fetch layer because PDF *content* support is a separate
    # content-type concern — here we only prove the transport-level block is gone.
    AKAMAI_PDF = (
        "https://www.maytag.com/content/dam/global/documents/"
        "201801/techsheet-w10828193-revd.pdf"
    )

    @pytest.mark.asyncio
    async def test_generic_path_clears_akamai_403(self):
        """Asserted against `_transport`, which is what a caller actually gets.

        This guard used to run against ``common.guarded_fetch``.  That was
        correct until the generic path moved to wreq and left the httpx
        implementation with no production callers, at which point the guard was
        pinning a code path no user reaches: wreq could have stopped
        negotiating HTTP/2 to Akamai and this would still have passed.
        """
        from parkour_mcp._transport import guarded_fetch as wreq_fetch

        resp = await wreq_fetch(self.AKAMAI_PDF, max_bytes=None)
        assert resp.status_code == 200
        assert resp.http_version == "HTTP/2"
        assert resp.content[:5] == b"%PDF-"


class TestLiveCloudflareChallenge:
    """A browser-coherent fingerprint clears a strict Cloudflare zone.

    The counterpart to the Akamai case above, and the reason the generic path
    moved off httpx.  Both WAFs score the same coherence property and disagree
    about which pairing is incoherent, so this pair has to pass together: a
    transport that clears one by breaking the other has not fixed anything.
    """

    # Zendesk-hosted, behind a Cloudflare zone running a strict Managed
    # Challenge.  httpx over HTTP/2 draws a 403 carrying
    # `cf-mitigated: challenge` because the h2 fingerprint contradicts the
    # Chrome User-Agent.
    NZXT_ARTICLE = (
        "https://support.nzxt.com/hc/en-us/articles/"
        "40379376386203-H7-Flow-2024-Specs"
    )

    @pytest.mark.asyncio
    async def test_wreq_transport_clears_managed_challenge(self):
        from parkour_mcp._transport import guarded_fetch as wreq_fetch

        resp = await wreq_fetch(self.NZXT_ARTICLE)
        assert resp.status_code == 200
        assert "cf-mitigated" not in resp.headers
        assert "H7 Flow" in resp.text
        # Pinned alongside the Akamai case: both WAFs are satisfied over
        # HTTP/2, which is only possible because the fingerprint is coherent
        # rather than because one of them was placated by downgrading.
        assert resp.http_version == "HTTP/2"
        # The pin bound and was observed, not merely configured.
        assert resp.pinned is True
        assert resp.remote_addr


class TestLiveProxyDegradation:
    """`pinned` must tell the truth when a proxy resolves on our behalf.

    A proxy performs the resolution that actually reaches the network, so no
    local pin can bind and the address check degrades to advisory.  The risk
    this guards is a *false* claim: if the client reported the destination
    address rather than the proxy's, the peer would be found in the validated
    set and the response would assert a guarantee it does not have.
    """

    @staticmethod
    async def _connect_proxy(host, port):
        """A minimal CONNECT proxy, so the degradation path is exercised for real."""
        import asyncio
        import contextlib

        async def pipe(reader, writer):
            # OSError covers the reset/broken-pipe pair a tunnel sees on
            # normal teardown; anything else should still surface.
            with contextlib.suppress(OSError):
                while chunk := await reader.read(65536):
                    writer.write(chunk)
                    await writer.drain()
            writer.close()

        async def handle(client_reader, client_writer):
            request = await client_reader.readline()
            if not request.upper().startswith(b"CONNECT"):
                client_writer.close()
                return
            target = request.split()[1].decode()
            up_host, _, up_port = target.partition(":")
            while (await client_reader.readline()) not in (b"\r\n", b"", b"\n"):
                pass
            try:
                up_r, up_w = await asyncio.open_connection(up_host, int(up_port or 443))
            except OSError:
                client_writer.close()
                return
            client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await client_writer.drain()
            await asyncio.gather(pipe(client_reader, up_w), pipe(up_r, client_writer))

        return await asyncio.start_server(handle, host, port)

    @pytest.mark.asyncio
    async def test_proxied_fetch_reports_unpinned(self, monkeypatch):
        from parkour_mcp._transport import guarded_fetch as wreq_fetch

        server = await self._connect_proxy("127.0.0.1", 8899)
        async with server:
            monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8899")
            resp = await wreq_fetch("https://example.com/")

        assert resp.status_code == 200
        assert resp.pinned is False, "claimed a pin the proxy made impossible"
        assert resp.remote_addr is not None
        assert resp.remote_addr.startswith("127.0.0.1")

    @pytest.mark.asyncio
    async def test_unproxied_fetch_reports_pinned(self):
        """The control: without a proxy the same fetch is genuinely pinned."""
        from parkour_mcp._transport import guarded_fetch as wreq_fetch

        resp = await wreq_fetch("https://example.com/")
        assert resp.pinned is True

    @pytest.mark.asyncio
    async def test_configured_proxy_is_not_bypassed(self, monkeypatch):
        """An unreachable proxy must fail the fetch, never fall back to direct.

        Where the proxy *is* the egress control, silently bypassing it removes
        the control.  This is the trap the httpx path fell into: passing a
        custom transport made httpx skip every proxy variable.
        """
        from parkour_mcp._transport import TransportFailure
        from parkour_mcp._transport import guarded_fetch as wreq_fetch

        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
        with pytest.raises(TransportFailure):
            await wreq_fetch("https://example.com/")
